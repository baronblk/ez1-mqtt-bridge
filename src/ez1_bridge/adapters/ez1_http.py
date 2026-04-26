"""Async HTTP client for the APsystems EZ1 local API on TCP/8050.

Wraps the seven endpoints documented in
``docs/_reference/apsystems-ez1-local-api.md``: ``getDeviceInfo``,
``getOutputData``, ``getMaxPower``, ``setMaxPower``, ``getAlarm``,
``getOnOff``, ``setOnOff``. Returns the raw envelope dict for each call;
parsing/validation is the normalizer's job in
:mod:`ez1_bridge.domain.normalizer`.

Designed as an async context manager so the underlying
:class:`httpx.AsyncClient` is reused across the bridge's lifetime --
TCP keep-alive on the 45-90 ms WLAN round-trip is the cheapest
performance win available, and reconnecting per request would dominate
the poll budget.

Retry behaviour is deliberately *not* a blanket decorator. A single
classifier (:func:`_is_transient`) decides per exception type:

* :class:`httpx.ConnectError` → fail fast (device offline, hammering
  does not help; the next poll cycle will try again).
* :class:`httpx.TimeoutException` → retry with exponential backoff.
* :class:`httpx.HTTPStatusError` 5xx → retry with exponential backoff.
* :class:`httpx.HTTPStatusError` 4xx → fail fast (programming error,
  not transient).

Application-level "envelope says ``message="FAILED"``" handling lives
in the normalizer / poll service, not here — the adapter only owns
transport concerns.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Final, Self

import httpx

_DEFAULT_TIMEOUT: Final[float] = 5.0
_DEFAULT_MAX_ATTEMPTS: Final[int] = 3
_BACKOFF_BASE_SECONDS: Final[float] = 1.0
_BACKOFF_CAP_SECONDS: Final[float] = 300.0
# HTTP 5xx range — Server errors that the EZ1 may emit during transient
# overloads. Fenced as constants to silence ruff's PLR2004 magic-number lint
# and to make the intent explicit at the comparison site.
_HTTP_SERVER_ERROR_LO: Final[int] = 500
_HTTP_SERVER_ERROR_HI: Final[int] = 600


def _is_transient(exc: BaseException) -> bool:
    """Return ``True`` if the exception represents a transient transport error.

    Drives the retry decision in :meth:`EZ1Client._request`. Defined as a
    standalone function (not a method) so tests can exercise the policy
    without instantiating a client.
    """
    match exc:
        case httpx.TimeoutException():
            return True
        case httpx.HTTPStatusError() as e if (
            _HTTP_SERVER_ERROR_LO <= e.response.status_code < _HTTP_SERVER_ERROR_HI
        ):
            return True
        case _:
            return False


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff: 1.0 s, 2.0 s, 4.0 s, ... capped at 300 s.

    ``attempt`` is 1-indexed — the wait *after* the first failed attempt
    is :func:`_backoff_seconds(1)`.
    """
    return min(_BACKOFF_BASE_SECONDS * (2.0 ** (attempt - 1)), _BACKOFF_CAP_SECONDS)


class EZ1Client:
    """Async HTTP client for the APsystems EZ1 local API.

    Use as an async context manager so the underlying
    :class:`httpx.AsyncClient` is created (and torn down) exactly once::

        async with EZ1Client("192.168.3.24") as client:
            envelope = await client.get_output_data()

    Calling a method outside the context manager raises
    :class:`RuntimeError`.
    """

    def __init__(
        self,
        host: str,
        port: int = 8050,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        if not host:
            msg = "host must be a non-empty string"
            raise ValueError(msg)
        if max_attempts < 1:
            msg = "max_attempts must be >= 1"
            raise ValueError(msg)
        self._base_url = f"http://{host}:{port}"
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._client: httpx.AsyncClient | None = None

    @property
    def base_url(self) -> str:
        """Resolved base URL (``http://<host>:<port>``)."""
        return self._base_url

    async def __aenter__(self) -> Self:
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            msg = "EZ1Client must be used as an async context manager"
            raise RuntimeError(msg)
        return self._client

    async def _request(
        self,
        path: str,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """GET ``path`` with retry policy and return the parsed JSON envelope."""
        client = self._ensure_client()
        last_exc: BaseException | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await client.get(path, params=params)
                response.raise_for_status()
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
                if attempt < self._max_attempts and _is_transient(exc):
                    await asyncio.sleep(_backoff_seconds(attempt))
                    continue
                raise
            else:
                payload = response.json()
                if not isinstance(payload, dict):
                    msg = (
                        f"unexpected response shape from {path}: "
                        f"expected JSON object, got {type(payload).__name__}"
                    )
                    raise TypeError(msg)
                return payload
        # All attempts exhausted with transient failures — re-raise the last one.
        assert last_exc is not None  # noqa: S101 — invariant from the loop above.
        raise last_exc

    # --- Read endpoints --------------------------------------------------

    async def get_device_info(self) -> dict[str, Any]:
        """``GET /getDeviceInfo`` — device static metadata."""
        return await self._request("/getDeviceInfo")

    async def get_output_data(self) -> dict[str, Any]:
        """``GET /getOutputData`` — instantaneous power and energy readings."""
        return await self._request("/getOutputData")

    async def get_max_power(self) -> dict[str, Any]:
        """``GET /getMaxPower`` — currently configured output limit."""
        return await self._request("/getMaxPower")

    async def get_alarm(self) -> dict[str, Any]:
        """``GET /getAlarm`` — diagnostic alarm bits."""
        return await self._request("/getAlarm")

    async def get_on_off(self) -> dict[str, Any]:
        """``GET /getOnOff`` — operational status (note inverted semantics)."""
        return await self._request("/getOnOff")

    # --- Write endpoints -------------------------------------------------

    async def set_max_power(self, watts: int) -> dict[str, Any]:
        """``GET /setMaxPower?p=<watts>`` — set the inverter's output limit.

        ``watts`` must be within the device's ``minPower``/``maxPower``
        range; that bound check is a higher-layer concern (see
        :mod:`ez1_bridge.application.command_handler`) and is not
        enforced here.
        """
        return await self._request("/setMaxPower", params={"p": str(watts)})

    async def set_on_off(self, *, on: bool) -> dict[str, Any]:
        """``GET /setOnOff?status=<0|1>`` — turn the inverter on or off.

        The on/off bit is inverted on the wire (``0`` is *on*,
        ``1`` is *off*); this method takes a boolean ``on=True/False``
        and applies the mapping internally so callers do not have to
        memorize the inversion.
        """
        return await self._request("/setOnOff", params={"status": "0" if on else "1"})
