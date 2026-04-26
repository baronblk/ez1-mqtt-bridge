"""Immutable domain models for the inverter state.

All models are frozen, strict (no implicit type coercion), and forbid
extra inputs — once an :class:`InverterState` is constructed, downstream
code can rely on it not changing under its feet.

Power and energy aggregations are derived as :func:`computed_field`
properties so the source-of-truth stays the per-channel reading.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field


class PowerReading(BaseModel):
    """Instantaneous power readings in watts, per MPPT channel.

    ``ch1_w`` and ``ch2_w`` are the two independent inputs of the EZ1; an
    asymmetry between them is normal and diagnostically valuable
    (shading, panel defect).
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    ch1_w: float = Field(ge=0)
    ch2_w: float = Field(ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_w(self) -> float:
        """Sum of channel 1 and channel 2 power."""
        return self.ch1_w + self.ch2_w


class EnergyReading(BaseModel):
    """Cumulative energy readings in kWh, per MPPT channel.

    The EZ1 exposes two flavours: ``e1``/``e2`` since device cold start
    (used for the *today* reading even though it is technically *since
    last boot*) and ``te1``/``te2`` over the device lifetime. Either
    flavour maps onto this model — context lives at the call site.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    ch1_kwh: float = Field(ge=0)
    ch2_kwh: float = Field(ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_kwh(self) -> float:
        """Sum of channel 1 and channel 2 energy, rounded to 5 decimal places.

        Five decimals match the resolution the EZ1 firmware emits
        (``0.28731`` kWh). Rounding here keeps the aggregated value from
        gaining ghost precision via floating-point addition.
        """
        return round(self.ch1_kwh + self.ch2_kwh, 5)


class AlarmFlags(BaseModel):
    """Diagnostic alarm bits from the EZ1 ``getAlarm`` endpoint.

    All four bits are independent. ``True`` means the alarm is active —
    the on-the-wire ``"1"``/``"0"`` semantics are normalized in
    :mod:`ez1_bridge.domain.normalizer`.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    off_grid: bool
    """AC connection missing or unstable (``og``)."""

    output_fault: bool
    """AC-side output fault (``oe``)."""

    dc1_short: bool
    """DC short circuit on channel 1 (``isce1``)."""

    dc2_short: bool
    """DC short circuit on channel 2 (``isce2``)."""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def any_active(self) -> bool:
        """``True`` if any of the four alarm bits is set."""
        return any((self.off_grid, self.output_fault, self.dc1_short, self.dc2_short))


class InverterState(BaseModel):
    """Aggregated, normalized snapshot of the inverter's current state.

    Built once per poll cycle from four independent endpoints
    (``getOutputData``, ``getMaxPower``, ``getAlarm``, ``getOnOff``)
    plus a timestamp.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    ts: datetime
    """Timestamp the snapshot was taken — usually ``datetime.now(tz=UTC)``."""

    device_id: str = Field(min_length=1)
    """The EZ1 ``deviceId`` (e.g. ``"E17010000783"``)."""

    power: PowerReading
    energy_today: EnergyReading
    energy_lifetime: EnergyReading

    max_power_w: int = Field(ge=0)
    """Currently configured output limit, in watts (``getMaxPower``)."""

    status: Literal["on", "off"]
    """Operational status, normalized from the inverter's inverted ``0/1`` semantics."""

    alarms: AlarmFlags
