"""Integration-test fixtures: real Mosquitto brokers spun up via testcontainers.

Two flavours are exposed:

* ``mosquitto_broker`` — anonymous broker, used by the bulk of tests
  (publish, retain, etc.) — session-scoped, started once.
* ``mosquitto_auth_broker`` — broker with ``allow_anonymous false`` and a
  password file containing a single ``ez1user:ez1pass`` entry. Session
  scope as well; the password hash is generated in-process so tests stay
  hermetic.

If Docker is unavailable on the host (no daemon running, missing socket,
permission denied) every fixture skips its dependent tests — the unit
layer still exercises the publisher contract via mocks, and CI on Linux
runners always has Docker.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import socket
import tempfile
import time
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import NamedTuple

import pytest

try:
    from docker.errors import DockerException
    from testcontainers.core.container import DockerContainer
except ImportError as exc:  # pragma: no cover - missing dev dep
    pytest.skip(f"testcontainers not installed: {exc}", allow_module_level=True)


_MOSQUITTO_IMAGE = "eclipse-mosquitto:2.0.20"
_DEFAULT_CONFIG = """\
listener 1883
allow_anonymous true
persistence false
log_dest stdout
"""
_AUTH_CONFIG = """\
listener 1883
allow_anonymous false
password_file /mosquitto/config/passwd
persistence false
log_dest stdout
"""
_READY_TIMEOUT_SECONDS = 30
_TCP_POLL_INTERVAL_SECONDS = 0.5

#: Credentials baked into the auth fixture. Hard-coded for reproducibility;
#: the password is fictional and never used outside the test harness.
AUTH_USERNAME = "ez1user"
AUTH_PASSWORD = "ez1pass"


class BrokerEndpoint(NamedTuple):
    """Resolved host/port pair for a running broker."""

    host: str
    port: int


def _mosquitto_password_hash(password: str, *, iterations: int = 101) -> str:
    """Build a Mosquitto ``$7$`` (PBKDF2-SHA512) password hash for ``password``.

    Format: ``$7$<iterations>$<salt_b64>$<hash_b64>`` — matches the format
    that ``mosquitto_passwd`` itself produces, so we do not need to run
    that binary inside the container.
    """
    salt = secrets.token_bytes(12)
    derived = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, iterations, dklen=64)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    hash_b64 = base64.b64encode(derived).decode("ascii")
    return f"$7${iterations}${salt_b64}${hash_b64}"


def _wait_for_tcp(host: str, port: int, timeout: float) -> None:
    """Block until ``host:port`` accepts a TCP connection or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    last_exc: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError as exc:
            last_exc = exc
            time.sleep(_TCP_POLL_INTERVAL_SECONDS)
    msg = f"broker {host}:{port} did not accept connections within {timeout}s"
    raise TimeoutError(msg) from last_exc


def _start_mosquitto(
    config_text: str,
    extra_files: dict[str, str] | None = None,
) -> tuple[DockerContainer, BrokerEndpoint]:
    """Spin up a Mosquitto container with the supplied config and extra files.

    ``extra_files`` maps relative filenames inside ``/mosquitto/config`` to
    their text content (e.g. a password file).

    The temp dir and its files are explicitly chmod'd to ``0755``/``0644``
    so the unprivileged ``mosquitto`` user (UID 1883) inside the container
    can read them. macOS Docker Desktop is forgiving about host-side
    permissions, but Linux Docker is not — without this, the auth broker
    silently fails to load the password file and rejects every CONNECT.
    """
    config_dir = Path(tempfile.mkdtemp(prefix="ez1-mosquitto-"))
    config_dir.chmod(0o755)
    (config_dir / "mosquitto.conf").write_text(config_text, encoding="utf-8")
    (config_dir / "mosquitto.conf").chmod(0o644)
    for name, content in (extra_files or {}).items():
        path = config_dir / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o644)

    container = (
        DockerContainer(_MOSQUITTO_IMAGE)
        .with_exposed_ports(1883)
        .with_volume_mapping(str(config_dir), "/mosquitto/config", "ro")
    )
    container.start()
    host = container.get_container_host_ip()
    port = int(container.get_exposed_port(1883))
    _wait_for_tcp(host, port, _READY_TIMEOUT_SECONDS)
    return container, BrokerEndpoint(host=host, port=port)


@pytest.fixture(scope="session")
def mosquitto_broker() -> Iterator[BrokerEndpoint]:
    """Session-scoped anonymous Mosquitto broker — shared across tests."""
    try:
        container, endpoint = _start_mosquitto(_DEFAULT_CONFIG)
    except DockerException as exc:
        pytest.skip(f"Docker not available for integration tests: {exc}")

    try:
        yield endpoint
    finally:
        with suppress(Exception):
            container.stop()


@pytest.fixture(scope="session")
def mosquitto_auth_broker() -> Iterator[BrokerEndpoint]:
    """Session-scoped Mosquitto broker with username/password authentication."""
    password_line = f"{AUTH_USERNAME}:{_mosquitto_password_hash(AUTH_PASSWORD)}\n"
    try:
        container, endpoint = _start_mosquitto(
            _AUTH_CONFIG,
            extra_files={"passwd": password_line},
        )
    except DockerException as exc:
        pytest.skip(f"Docker not available for integration tests: {exc}")

    try:
        yield endpoint
    finally:
        with suppress(Exception):
            container.stop()
