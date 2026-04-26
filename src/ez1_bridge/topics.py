"""Centralized MQTT topic builders for the bridge.

Every topic string in the codebase flows through this module — there are no
magic strings anywhere else. Phase 4 (HA discovery) and Phase 5 (command
handler) import the same builders so a topic-schema change ripples from
exactly one place.

Retain semantics
----------------

The ``RETAIN`` mapping below fixes the per-topic-kind retain behaviour as
machine-readable metadata. The publisher reads it directly; readers of
this module do not have to look up MQTT semantics elsewhere.

================  ======  ========================================================
Topic kind        Retain  Reason
================  ======  ========================================================
availability      ✅      New subscribers must see liveness state immediately.
state             ✅      Home Assistant needs latest state after a broker restart.
flat              ✅      Same — for non-JSON consumers reading individual metrics.
result            ❌      Event-based; old write outcomes would be misleading.
discovery         ✅      HA discovery configs persist across HA restarts.
set (subscribed)  n/a     Subscribed by the bridge, never published from here.
================  ======  ========================================================
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal

# --- Static path components --------------------------------------------
_AVAILABILITY: Final[str] = "availability"
_STATE: Final[str] = "state"
_RESULT: Final[str] = "result"
_SET: Final[str] = "set"
_CONFIG: Final[str] = "config"

# --- Availability payload constants ------------------------------------
AVAILABILITY_ONLINE: Final[str] = "online"
AVAILABILITY_OFFLINE: Final[str] = "offline"

# --- Type aliases ------------------------------------------------------
HAComponent = Literal["sensor", "binary_sensor"]


# --- Builders ----------------------------------------------------------


def availability(base: str, device_id: str) -> str:
    """``{base}/{device_id}/availability`` (retain=True, payload online/offline)."""
    return f"{base}/{device_id}/{_AVAILABILITY}"


def state(base: str, device_id: str) -> str:
    """``{base}/{device_id}/state`` (retain=True, JSON payload)."""
    return f"{base}/{device_id}/{_STATE}"


def flat(base: str, device_id: str, group: str, key: str) -> str:
    """``{base}/{device_id}/{group}/{key}`` for individual metric values.

    Used for the per-metric flat topics (e.g. ``ez1/E17.../power/total_w``)
    that complement the structured JSON state topic for non-JSON consumers.
    """
    return f"{base}/{device_id}/{group}/{key}"


def command(base: str, device_id: str, name: str) -> str:
    """``{base}/{device_id}/set/{name}`` (subscribed; not published from here)."""
    return f"{base}/{device_id}/{_SET}/{name}"


def command_wildcard(base: str, device_id: str) -> str:
    """``{base}/{device_id}/set/+`` — wildcard subscription pattern."""
    return f"{base}/{device_id}/{_SET}/+"


def result(base: str, device_id: str, name: str) -> str:
    """``{base}/{device_id}/result/{name}`` (retain=False, event-based)."""
    return f"{base}/{device_id}/{_RESULT}/{name}"


def discovery(prefix: str, component: HAComponent, device_id: str, key: str) -> str:
    """``{prefix}/{component}/{device_id}/{key}/config`` for HA discovery.

    Components are constrained to the two HA platform types we use:
    ``sensor`` (numeric metrics) and ``binary_sensor`` (alarm bits).
    """
    return f"{prefix}/{component}/{device_id}/{key}/{_CONFIG}"


# --- Retain semantics, machine-readable --------------------------------

RETAIN: Final[Mapping[str, bool]] = {
    "availability": True,
    "state": True,
    "flat": True,
    "result": False,
    "discovery": True,
}
