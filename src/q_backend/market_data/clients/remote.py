"""Linux-side client for the remote MT5 data gateway (WO183/WO184).

``RemoteMt5Client`` speaks the gateway's ``/v1/`` wire contract over HTTP and behaves
exactly like ``MetaTraderClient`` from a consumer's point of view: same methods, same
return types, same naive-Brasília datetimes on ``OHLCV.time`` / ``Tick.time``. It
satisfies the runtime-checkable ``MarketDataProvider`` protocol.

Configuration resolution (constructor arg -> env -> runtime config):
  * ``base_url``: explicit arg, else ``get_remote_gateway_url()`` which is
    ``Q_MT5_GATEWAY_URL`` env, then the ``remote_gateway_url`` JSON config key, else None.
  * ``token``: explicit arg, else ``get_remote_gateway_token()`` (``Q_MT5_GATEWAY_TOKEN``
    env, then ``remote_gateway_token`` JSON config key, else None). When set, every
    request carries the ``X-Gateway-Token`` header.

Timezone convention is sacred: outbound ``start``/``end`` are normalized with
``to_brasilia_naive`` and sent as ISO strings; inbound raw epochs are converted back
with ``unix_seconds_to_brasilia_naive`` — no new timezone code lives here.

Exception mapping (gateway response -> raised):
  * network error / timeout            -> ``ConnectionError``
  * HTTP 503 / code ``mt5_unavailable``-> ``ConnectionError``
  * HTTP 400 (bad params)              -> ``ValueError``
  * HTTP 404 on ``get_symbol_info`` / ``get_available_ohlcv_range`` -> ``None``
  * HTTP 404 ``symbol_not_found`` on ``get_ohlcv`` / ``get_ticks*`` -> empty result
    (mirrors ``MetaTraderClient``, which returns ``[]`` for an unselectable symbol)
  * any other non-2xx                  -> ``ConnectionError``
"""

from __future__ import annotations

import io
import logging
import threading
import time
from datetime import datetime
from typing import Any, Optional

import httpx
import numpy as np

from q_backend.market_data.clients.metatrader import (
    COPY_TICKS_ALL,
    COPY_TICKS_TRADE,
    OhlcvAvailableRange,
    _RECENT_TICKS_WINDOWS,
    _empty_ticks_columnar,
)
from q_backend.market_data.columnar import columnar_to_ticks
from q_backend.market_data.models import OHLCV, Tick
from q_backend.market_data.tick_cache import load as load_tick_cache
from q_backend.market_data.tick_cache import make_cache_key
from q_backend.market_data.tick_cache import store as store_tick_cache
from q_backend.market_data.timezone import (
    to_brasilia_naive,
    unix_seconds_to_brasilia_naive,
)
from q_backend.storage.runtime_config import (
    get_remote_gateway_token,
    get_remote_gateway_url,
)

logger = logging.getLogger(__name__)

_PROVIDER = "remote"


class _GatewayNotFound(Exception):
    """Internal signal for an HTTP 404 from the gateway (never leaks to callers)."""


class RemoteMt5Client:
    """MarketDataProvider backed by a remote MT5 HTTP gateway."""

    #: Wire schema major this client understands (see gateway ``SCHEMA_VERSION``).
    SUPPORTED_SCHEMA_MAJOR = 1

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = 30.0,
        health_timeout: float = 1.0,
        health_cache_seconds: float = 30.0,
    ):
        resolved_url = base_url if base_url is not None else get_remote_gateway_url()
        self._base_url = resolved_url.rstrip("/") if resolved_url else None
        self._token = token if token is not None else get_remote_gateway_token()
        self._timeout = timeout
        self._health_timeout = health_timeout
        self._health_cache_seconds = health_cache_seconds
        self._lock = threading.Lock()
        # (monotonic_deadline, available) or None when unknown.
        self._health_cache: tuple[float, bool] | None = None

    # -- capability / connectivity -----------------------------------------
    def is_supported(self) -> bool:
        """True iff a gateway URL is configured (routing eligibility)."""
        return self._base_url is not None

    def is_available(self) -> bool:
        """Health probe with a 30s monotonic cache; never raises."""
        with self._lock:
            now = time.monotonic()
            if self._health_cache is not None:
                deadline, available = self._health_cache
                if now < deadline:
                    return available
            available = self._probe_health()
            self._health_cache = (now + self._health_cache_seconds, available)
            return available

    def connect(self) -> bool:
        """Force a fresh health probe and refresh the cache."""
        with self._lock:
            available = self._probe_health()
            self._health_cache = (
                time.monotonic() + self._health_cache_seconds,
                available,
            )
            return available

    def disconnect(self) -> None:
        """Drop the cached health state."""
        with self._lock:
            self._health_cache = None

    def _probe_health(self) -> bool:
        if not self._base_url:
            return False
        try:
            with httpx.Client(
                base_url=self._base_url,
                timeout=self._health_timeout,
                headers=self._headers(),
            ) as client:
                resp = client.get("/v1/health")
        except httpx.HTTPError as exc:
            logger.debug("Remote MT5 gateway health probe failed: %s", exc)
            return False

        if resp.status_code != 200:
            logger.debug("Remote MT5 gateway health returned HTTP %s", resp.status_code)
            return False

        try:
            payload = resp.json()
        except ValueError:
            logger.error("Remote MT5 gateway health returned non-JSON body")
            return False

        version = payload.get("schema_version")
        if not self._schema_compatible(version):
            logger.error(
                "Remote MT5 gateway schema_version %r is incompatible with client "
                "major %s; treating gateway as unavailable.",
                version,
                self.SUPPORTED_SCHEMA_MAJOR,
            )
            return False

        return True

    def _schema_compatible(self, version: Any) -> bool:
        if not isinstance(version, str) or not version:
            return False
        major = version.split(".", 1)[0]
        try:
            return int(major) == self.SUPPORTED_SCHEMA_MAJOR
        except ValueError:
            return False

    # -- OHLCV --------------------------------------------------------------
    def get_ohlcv(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[OHLCV]:
        params = {
            "symbol": symbol,
            "timeframe": timeframe,
            "start": to_brasilia_naive(start).isoformat(),
            "end": to_brasilia_naive(end).isoformat(),
        }
        try:
            content = self._get_npz("/v1/ohlcv", params)
        except _GatewayNotFound:
            return []
        with np.load(io.BytesIO(content)) as npz:
            return _npz_to_ohlcv(npz)

    def get_available_ohlcv_range(self, symbol: str, timeframe: str) -> Optional[OhlcvAvailableRange]:
        try:
            payload = self._get_json("/v1/available_range", {"symbol": symbol, "timeframe": timeframe})
        except _GatewayNotFound:
            return None
        return OhlcvAvailableRange(
            symbol=payload["symbol"],
            timeframe=payload["timeframe"],
            start=datetime.fromisoformat(payload["start"]),
            end=datetime.fromisoformat(payload["end"]),
            bar_count=int(payload["bar_count"]),
        )

    # -- ticks --------------------------------------------------------------
    def get_ticks_columnar(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        flags: int | None = None,
        use_cache: bool = True,
    ) -> dict[str, np.ndarray]:
        start_local = to_brasilia_naive(start)
        end_local = to_brasilia_naive(end)
        resolved_flags = COPY_TICKS_ALL if flags is None else flags

        cache_key: str | None = None
        if use_cache:
            cache_key = make_cache_key(symbol, start_local, end_local, resolved_flags, provider=_PROVIDER)
            cached = load_tick_cache(cache_key)
            if cached is not None:
                return cached

        params = {
            "symbol": symbol,
            "start": start_local.isoformat(),
            "end": end_local.isoformat(),
            "flags": _flags_to_str(resolved_flags),
        }
        try:
            content = self._get_npz("/v1/ticks", params)
        except _GatewayNotFound:
            return _empty_ticks_columnar()

        with np.load(io.BytesIO(content)) as npz:
            result = _npz_to_columnar(npz)

        if use_cache and cache_key is not None:
            store_tick_cache(cache_key, result)
        return result

    def get_ticks(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        flags: int | None = None,
    ) -> list[Tick]:
        arrays = self.get_ticks_columnar(symbol, start, end, flags=flags, use_cache=False)
        return columnar_to_ticks(arrays)

    def get_recent_ticks(self, symbol: str, limit: int = 200) -> list[Tick]:
        end = datetime.now()
        mapped: list[Tick] = []
        for window in _RECENT_TICKS_WINDOWS:
            arrays = self.get_ticks_columnar(
                symbol,
                end - window,
                end,
                flags=COPY_TICKS_ALL,
                use_cache=False,
            )
            mapped = columnar_to_ticks(arrays)
            if len(mapped) >= limit:
                break
        return mapped[-limit:]

    # -- symbols ------------------------------------------------------------
    def search_symbols(self, query: str) -> list[dict[str, Any]]:
        return self._get_json("/v1/symbols/search", {"query": query})

    def get_symbol_info(self, symbol: str) -> dict[str, Any] | None:
        try:
            return self._get_json("/v1/symbol_info", {"symbol": symbol})
        except _GatewayNotFound:
            return None

    # -- HTTP plumbing ------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {"X-Gateway-Token": self._token} if self._token else {}

    def _get(self, path: str, params: dict[str, Any]) -> httpx.Response:
        if not self._base_url:
            raise ConnectionError("No remote MT5 gateway URL is configured.")
        try:
            with httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout,
                headers=self._headers(),
            ) as client:
                resp = client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ConnectionError(f"Remote MT5 gateway request to {path} failed: {exc}") from exc

        if resp.status_code == 200:
            return resp
        self._raise_for_status(path, resp)

    def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        resp = self._get(path, params)
        try:
            return resp.json()
        except ValueError as exc:
            raise ConnectionError(f"Remote MT5 gateway {path} returned a non-JSON body.") from exc

    def _get_npz(self, path: str, params: dict[str, Any]) -> bytes:
        return self._get(path, params).content

    def _raise_for_status(self, path: str, resp: httpx.Response) -> None:
        code, message = _error_detail(resp)
        status = resp.status_code
        if status == 404:
            raise _GatewayNotFound(message or f"{path} not found")
        if status == 400:
            raise ValueError(message or f"Bad request to {path}.")
        if status == 503 or code == "mt5_unavailable":
            raise ConnectionError(message or "Remote MT5 gateway reports the terminal is unavailable.")
        raise ConnectionError(f"Remote MT5 gateway {path} error (HTTP {status}): {message}")


def _flags_to_str(flags: int) -> str:
    return "trade" if flags == COPY_TICKS_TRADE else "all"


def _error_detail(resp: httpx.Response) -> tuple[str | None, str | None]:
    try:
        body = resp.json()
    except ValueError:
        return None, resp.text or None
    if isinstance(body, dict):
        return body.get("code"), body.get("error")
    return None, None


def _npz_to_ohlcv(npz) -> list[OHLCV]:
    time_arr = npz["time"]
    has_spread = "spread" in npz.files
    has_real_volume = "real_volume" in npz.files
    open_ = npz["open"]
    high = npz["high"]
    low = npz["low"]
    close = npz["close"]
    tick_volume = npz["tick_volume"]
    spread = npz["spread"] if has_spread else None
    real_volume = npz["real_volume"] if has_real_volume else None

    return [
        OHLCV(
            time=unix_seconds_to_brasilia_naive(int(time_arr[i])),
            open=float(open_[i]),
            high=float(high[i]),
            low=float(low[i]),
            close=float(close[i]),
            tick_volume=int(tick_volume[i]),
            spread=int(spread[i]) if spread is not None else None,
            real_volume=int(real_volume[i]) if real_volume is not None else None,
        )
        for i in range(len(time_arr))
    ]


def _npz_to_columnar(npz) -> dict[str, np.ndarray]:
    return {
        "time_msc": np.asarray(npz["time_msc"], dtype=np.int64),
        "bid": np.asarray(npz["bid"], dtype=np.float64),
        "ask": np.asarray(npz["ask"], dtype=np.float64),
        "last": np.asarray(npz["last"], dtype=np.float64),
        "volume": np.asarray(npz["volume"], dtype=np.float64),
        "flags": np.asarray(npz["flags"], dtype=np.int32),
    }
