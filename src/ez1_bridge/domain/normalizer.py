"""Normalize raw EZ1 API payloads into :class:`InverterState` domain models.

Implementation lands in Phase 1.

Backlog (Phase 2)
-----------------
* **Inverted on/off semantics.** The EZ1 API uses ``status="0"`` for *on* and
  ``status="1"`` for *off* — the opposite of intuition. Centralize the mapping
  in a single module-level constant
  ``_STATUS_MAP: Final[Mapping[str, Literal["on", "off"]]] = {"0": "on", "1": "off"}``
  and cover it with a parameterized test in
  ``tests/unit/test_normalizer.py`` (table-driven over both directions). This
  makes future refactorings structurally safe.
* **String coercion for power values.** ``minPower`` and ``maxPower`` arrive as
  strings (e.g. ``"800"``) in the JSON envelope. Define a private helper
  ``_to_int_watt(v: str) -> int`` that raises :class:`ValueError` for
  non-numeric input. Do *not* rely on Pydantic's implicit numeric coercion —
  this defends against firmware updates that emit ``"800W"`` or ``"800.0"``.
"""
