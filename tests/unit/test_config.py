"""Tests for :class:`ez1_bridge.config.Settings`.

Covers default values, env-var overrides, validation failures, frozenness,
and — critically — the ``SecretStr`` discipline: the MQTT password must
never appear in :func:`repr`, :meth:`str`, or :meth:`model_dump_json`
output. Without this guarantee, structlog or any other consumer that
binds the settings object as logging context will silently leak the
secret.
"""

from __future__ import annotations

import os

import pytest
from pydantic import SecretStr, ValidationError

from ez1_bridge.config import Settings


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Strip any pre-existing ``EZ1_BRIDGE_*`` env vars so tests start clean."""
    for key in list(os.environ):
        if key.startswith("EZ1_BRIDGE_"):
            monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _make(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    """Construct ``Settings`` from explicit env vars only (ignores any .env)."""
    for k, v in env.items():
        monkeypatch.setenv(f"EZ1_BRIDGE_{k.upper()}", v)
    return Settings(_env_file=None)  # type: ignore[call-arg]


# --- defaults -------------------------------------------------------------


def test_defaults_with_only_required_fields_set(isolated_env: pytest.MonkeyPatch) -> None:
    settings = _make(isolated_env, ez1_host="192.168.3.24", mqtt_host="192.168.2.10")

    assert settings.ez1_host == "192.168.3.24"
    assert settings.ez1_port == 8050
    assert settings.poll_interval == 20
    assert settings.request_timeout == 5
    assert settings.mqtt_host == "192.168.2.10"
    assert settings.mqtt_port == 1883
    assert settings.mqtt_user is None
    assert settings.mqtt_password is None
    assert settings.mqtt_base_topic == "ez1"
    assert settings.mqtt_discovery_prefix == "homeassistant"
    assert settings.metrics_port == 9100
    assert settings.log_level == "INFO"
    assert settings.log_format == "auto"
    assert settings.setmaxpower_verify is True


def test_setmaxpower_verify_can_be_disabled(isolated_env: pytest.MonkeyPatch) -> None:
    settings = _make(
        isolated_env,
        ez1_host="192.168.3.24",
        mqtt_host="192.168.2.10",
        setmaxpower_verify="false",
    )
    assert settings.setmaxpower_verify is False


def test_required_fields_must_be_set(isolated_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)  # type: ignore[call-arg]

    errors = excinfo.value.errors()
    missing = {tuple(err["loc"]) for err in errors if err["type"] == "missing"}
    assert ("ez1_host",) in missing
    assert ("mqtt_host",) in missing


# --- env override -------------------------------------------------------


def test_all_fields_overridable_via_env(isolated_env: pytest.MonkeyPatch) -> None:
    settings = _make(
        isolated_env,
        ez1_host="10.0.0.1",
        ez1_port="9000",
        poll_interval="30",
        request_timeout="10",
        mqtt_host="broker.local",
        mqtt_port="8883",
        mqtt_user="alice",
        mqtt_password="s3cret",
        mqtt_base_topic="solar",
        mqtt_discovery_prefix="ha",
        metrics_port="9200",
        log_level="DEBUG",
        log_format="json",
    )

    assert settings.ez1_host == "10.0.0.1"
    assert settings.ez1_port == 9000
    assert settings.poll_interval == 30
    assert settings.request_timeout == 10
    assert settings.mqtt_host == "broker.local"
    assert settings.mqtt_port == 8883
    assert settings.mqtt_base_topic == "solar"
    assert settings.mqtt_discovery_prefix == "ha"
    assert settings.metrics_port == 9200
    assert settings.log_level == "DEBUG"
    assert settings.log_format == "json"
    assert isinstance(settings.mqtt_user, SecretStr)
    assert settings.mqtt_user.get_secret_value() == "alice"
    assert isinstance(settings.mqtt_password, SecretStr)
    assert settings.mqtt_password.get_secret_value() == "s3cret"


# --- SecretStr discipline -----------------------------------------------


def test_secret_password_does_not_leak_in_repr(isolated_env: pytest.MonkeyPatch) -> None:
    settings = _make(
        isolated_env,
        ez1_host="192.168.3.24",
        mqtt_host="192.168.2.10",
        mqtt_password="hunter2",
    )

    rendered = repr(settings)
    assert "hunter2" not in rendered
    assert "**********" in rendered or "SecretStr" in rendered


def test_secret_password_does_not_leak_in_str(isolated_env: pytest.MonkeyPatch) -> None:
    settings = _make(
        isolated_env,
        ez1_host="192.168.3.24",
        mqtt_host="192.168.2.10",
        mqtt_password="hunter2",
    )

    assert "hunter2" not in str(settings)


def test_secret_password_does_not_leak_in_model_dump_json(
    isolated_env: pytest.MonkeyPatch,
) -> None:
    settings = _make(
        isolated_env,
        ez1_host="192.168.3.24",
        mqtt_host="192.168.2.10",
        mqtt_password="hunter2",
        mqtt_user="alice",
    )

    dumped = settings.model_dump_json()
    assert "hunter2" not in dumped
    # Username is not a secret per spec, but if marked SecretStr it must
    # also not leak — guarded here so a future refactor doesn't regress.
    assert "alice" not in dumped


def test_secret_password_does_not_leak_in_model_dump(
    isolated_env: pytest.MonkeyPatch,
) -> None:
    settings = _make(
        isolated_env,
        ez1_host="192.168.3.24",
        mqtt_host="192.168.2.10",
        mqtt_password="hunter2",
    )

    dumped = settings.model_dump()
    # SecretStr survives a dict dump as a SecretStr instance, not a raw string.
    assert isinstance(dumped["mqtt_password"], SecretStr)
    assert "hunter2" not in str(dumped)


# --- validation ---------------------------------------------------------


def test_invalid_log_level_rejected(isolated_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        _make(
            isolated_env,
            ez1_host="192.168.3.24",
            mqtt_host="192.168.2.10",
            log_level="TRACE",
        )


def test_invalid_log_format_rejected(isolated_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        _make(
            isolated_env,
            ez1_host="192.168.3.24",
            mqtt_host="192.168.2.10",
            log_format="syslog",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("poll_interval", "0"),
        ("poll_interval", "-1"),
        ("request_timeout", "0"),
        ("ez1_port", "0"),
        ("ez1_port", "70000"),
        ("mqtt_port", "0"),
        ("metrics_port", "0"),
    ],
)
def test_out_of_range_numeric_fields_rejected(
    isolated_env: pytest.MonkeyPatch, field: str, value: str
) -> None:
    with pytest.raises(ValidationError):
        _make(
            isolated_env,
            ez1_host="192.168.3.24",
            mqtt_host="192.168.2.10",
            **{field: value},
        )


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("mqtt_base_topic", ""),
        ("mqtt_base_topic", "ez1/with/slash"),
        ("mqtt_base_topic", "ez1#"),
        ("mqtt_base_topic", "ez1+"),
        ("mqtt_base_topic", "ez1 with space"),
        ("mqtt_discovery_prefix", "ha/lights"),
    ],
)
def test_topic_field_rejects_mqtt_wildcards_and_separators(
    isolated_env: pytest.MonkeyPatch, field: str, bad_value: str
) -> None:
    with pytest.raises(ValidationError):
        _make(
            isolated_env,
            ez1_host="192.168.3.24",
            mqtt_host="192.168.2.10",
            **{field: bad_value},
        )


def test_extra_constructor_kwargs_rejected(isolated_env: pytest.MonkeyPatch) -> None:
    """``extra="forbid"`` guards against typos in programmatic Settings construction.

    Note: pydantic-settings *does not* surface unknown prefixed env vars as
    extra inputs — they are silently filtered out before the model sees
    them. Detecting env-var typos would require a separate scan over
    ``os.environ`` and is left out of scope; the constructor-side guard
    catches the most common mistake (passing a bogus kwarg from code).
    """
    isolated_env.setenv("EZ1_BRIDGE_EZ1_HOST", "192.168.3.24")
    isolated_env.setenv("EZ1_BRIDGE_MQTT_HOST", "192.168.2.10")

    with pytest.raises(ValidationError):
        Settings(_env_file=None, foobar="oops")  # type: ignore[call-arg]


# --- frozenness ---------------------------------------------------------


def test_settings_is_frozen(isolated_env: pytest.MonkeyPatch) -> None:
    settings = _make(isolated_env, ez1_host="192.168.3.24", mqtt_host="192.168.2.10")

    with pytest.raises(ValidationError):
        settings.ez1_port = 9999
