"""Tests for :mod:`ez1_bridge.logging_setup`."""

from __future__ import annotations

import io
import json
import logging
import sys

import pytest
import structlog

from ez1_bridge.logging_setup import configure_logging, resolve_format

# --- resolve_format ---------------------------------------------------


def test_resolve_format_passes_through_explicit_settings() -> None:
    assert resolve_format("json") == "json"
    assert resolve_format("text") == "text"


def test_resolve_format_auto_picks_json_for_non_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert resolve_format("auto") == "json"


def test_resolve_format_auto_picks_text_for_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_tty = io.StringIO()
    fake_tty.isatty = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stderr", fake_tty)
    assert resolve_format("auto") == "text"


# --- configure_logging -----------------------------------------------


def test_configure_logging_json_emits_valid_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="INFO", format_="json")
    log = structlog.get_logger("test")
    log.info("hello", foo="bar")

    captured = capsys.readouterr()
    line = captured.out or captured.err
    assert line  # the renderer prints somewhere
    parsed = json.loads(line.strip().splitlines()[-1])
    assert parsed["event"] == "hello"
    assert parsed["foo"] == "bar"
    assert parsed["level"] == "info"
    assert "timestamp" in parsed


def test_configure_logging_text_format_does_not_crash() -> None:
    configure_logging(level="DEBUG", format_="text")
    log = structlog.get_logger("test")
    log.debug("text mode", x=1)


def test_configure_logging_levels_filter_messages(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A WARNING level filter must drop INFO and DEBUG events."""
    configure_logging(level="WARNING", format_="json")
    log = structlog.get_logger("test")

    log.debug("dropped_debug")
    log.info("dropped_info")
    log.warning("kept_warning", value=42)

    captured = capsys.readouterr()
    output = (captured.out + captured.err).strip()
    assert "dropped_debug" not in output
    assert "dropped_info" not in output
    assert "kept_warning" in output


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR"])
def test_configure_logging_accepts_all_levels(level: str) -> None:
    configure_logging(level=level, format_="json")  # type: ignore[arg-type]


def test_configure_logging_resets_between_calls(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reconfiguring should change the active level immediately."""
    configure_logging(level="ERROR", format_="json")
    log = structlog.get_logger("test")
    log.info("hidden")
    capsys.readouterr()

    configure_logging(level="INFO", format_="json")
    log = structlog.get_logger("test")
    log.info("visible")
    captured = capsys.readouterr()
    assert "visible" in (captured.out + captured.err)


def test_configure_logging_processor_order_includes_timestamp(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The TimeStamper processor must populate an iso-format ``timestamp`` key."""
    configure_logging(level="INFO", format_="json")
    log = structlog.get_logger("test")
    log.info("timed")

    captured = capsys.readouterr()
    line = (captured.out + captured.err).strip().splitlines()[-1]
    parsed = json.loads(line)
    timestamp = parsed["timestamp"]
    # ISO 8601 with timezone marker
    assert "T" in timestamp
    assert timestamp.endswith("Z") or "+" in timestamp


def test_configure_logging_renders_exc_info(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A logger used inside ``except`` should serialise the traceback."""
    configure_logging(level="INFO", format_="json")
    log = structlog.get_logger("test")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log.error("caught", exc_info=True)

    captured = capsys.readouterr()
    line = (captured.out + captured.err).strip().splitlines()[-1]
    parsed = json.loads(line)
    assert "RuntimeError" in parsed.get("exception", "")


def test_configure_logging_via_stdlib_does_not_crash() -> None:
    """Stdlib loggers continue to work alongside structlog (best-effort)."""
    configure_logging(level="INFO", format_="json")
    stdlib = logging.getLogger("test.stdlib")
    stdlib.info("stdlib message")
