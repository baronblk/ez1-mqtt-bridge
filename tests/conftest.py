"""Shared pytest fixtures for the ez1-mqtt-bridge test suite."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "api_responses"


def _load(name: str) -> dict[str, Any]:
    """Load a verified EZ1 API JSON payload from ``tests/fixtures/api_responses``."""
    path = _FIXTURE_DIR / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = f"fixture {name!r} must contain a JSON object at the top level"
        raise TypeError(msg)
    return data


@pytest.fixture(scope="session")
def api_response() -> Callable[[str], dict[str, Any]]:
    """Loader for verified EZ1 API JSON payloads.

    Usage::

        def test_x(api_response):
            payload = api_response("get_output_data")

    The five available fixture names mirror the five EZ1 read endpoints:
    ``get_device_info``, ``get_output_data``, ``get_max_power``,
    ``get_alarm``, ``get_on_off``.
    """
    return _load
