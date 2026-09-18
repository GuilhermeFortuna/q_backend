"""HTTP client for the MT5 execution edge (Q-042)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx

from q_contracts.edge import AccountResponse, EdgeHealthResponse, ExecutionOrder, QuoteResponse

logger = logging.getLogger(__name__)

SUPPORTED_SCHEMA_MAJOR = 1


class EdgeUnavailable(Exception):
    """Raised when the edge cannot be reached or refuses the request."""


@dataclass(frozen=True)
class EdgeTimeouts:
    connect_s: float = 1.0
    read_s: float = 5.0
    submit_read_s: float = 15.0


def _schema_major(schema_version: str) -> int:
    return int(str(schema_version).split(".", 1)[0])


def _iso_timestamp(value: datetime) -> str:
    ts = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return ts.isoformat().replace("+00:00", "Z")


class EdgeClient:
    """Typed client for the execution edge wire contract."""

    def __init__(
        self,
        base_url: str,
        *,
        timeouts: EdgeTimeouts,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeouts = timeouts
        self._transport = transport

    def _client(self, *, read_timeout: float) -> httpx.Client:
        timeout = httpx.Timeout(read_timeout, connect=self._timeouts.connect_s)
        return httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            transport=self._transport,
            headers={"X-Schema-Major": str(SUPPORTED_SCHEMA_MAJOR)},
        )

    def _check_schema_major(self, payload: dict[str, Any]) -> None:
        major = _schema_major(str(payload.get("schema_version", "")))
        if major != SUPPORTED_SCHEMA_MAJOR:
            raise EdgeUnavailable(f"edge schema major {major} does not match supported major {SUPPORTED_SCHEMA_MAJOR}")

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        read_timeout: float,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            with self._client(read_timeout=read_timeout) as client:
                response = client.request(method, path, params=params, json=json_body)
        except httpx.HTTPError as exc:
            raise EdgeUnavailable(str(exc)) from exc

        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            code = payload.get("code", "unknown")
            message = payload.get("error", response.text)
            raise EdgeUnavailable(f"{code}: {message}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise EdgeUnavailable("edge returned non-JSON body") from exc
        if not isinstance(payload, dict):
            raise EdgeUnavailable("edge returned unexpected JSON shape")
        return payload

    def health(self) -> EdgeHealthResponse:
        payload = self._request_json("GET", "/v1/health", read_timeout=self._timeouts.read_s)
        self._check_schema_major(payload)
        return EdgeHealthResponse(
            status=str(payload["status"]),
            schema_version=str(payload["schema_version"]),
            mt5_connected=bool(payload["mt5_connected"]),
            terminal_build=payload.get("terminal_build"),
        )

    def account(self) -> AccountResponse:
        payload = self._request_json("GET", "/v1/account", read_timeout=self._timeouts.read_s)
        return AccountResponse(
            balance=float(payload["balance"]),
            currency=str(payload["currency"]),
            equity=float(payload["equity"]),
            login=int(payload["login"]),
            margin_free=float(payload["margin_free"]),
            server=str(payload["server"]),
            terminal_trade_allowed=bool(payload["terminal_trade_allowed"]),
            trade_allowed=bool(payload["trade_allowed"]),
        )

    def quote(self, symbol: str) -> QuoteResponse:
        payload = self._request_json(
            "GET",
            "/v1/quote",
            read_timeout=self._timeouts.read_s,
            params={"symbol": symbol},
        )
        return QuoteResponse(
            age_ms=int(payload["age_ms"]),
            ask=float(payload["ask"]),
            bid=float(payload["bid"]),
            last=float(payload["last"]),
            symbol=str(payload["symbol"]),
            time_msc=int(payload["time_msc"]),
        )

    def check(self, intent_id: UUID, order: ExecutionOrder) -> dict[str, Any]:
        return self._request_json(
            "POST",
            "/v1/check",
            read_timeout=self._timeouts.read_s,
            json_body={"intent_id": str(intent_id), "order": _order_payload(order)},
        )

    def submit(self, intent_id: UUID, order: ExecutionOrder) -> dict[str, Any]:
        """Submit once; transport failures return indeterminate, never raise."""
        body = {"intent_id": str(intent_id), "order": _order_payload(order)}
        try:
            with self._client(read_timeout=self._timeouts.submit_read_s) as client:
                response = client.post("/v1/submit", json=body)
        except httpx.HTTPError as exc:
            logger.warning("edge submit transport failure for intent %s: %s", intent_id, exc)
            return {"outcome": "indeterminate", "reason": str(exc)}

        if response.status_code == 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            code = payload.get("code")
            if code == "duplicate_intent":
                logger.error(
                    "edge reported duplicate_intent for %s; invariant violated",
                    intent_id,
                )
                return {
                    "outcome": "indeterminate",
                    "reason": payload.get("error", "duplicate_intent"),
                }
            return {
                "outcome": "indeterminate",
                "reason": payload.get("error", f"HTTP {response.status_code}"),
            }

        if response.status_code >= 400:
            return {
                "outcome": "indeterminate",
                "reason": f"HTTP {response.status_code}: {response.text}",
            }

        try:
            payload = response.json()
        except ValueError as exc:
            return {"outcome": "indeterminate", "reason": f"non-JSON response: {exc}"}
        if not isinstance(payload, dict):
            return {"outcome": "indeterminate", "reason": "unexpected JSON shape"}
        return payload

    def lookup(
        self,
        intent_id: UUID,
        window_start: datetime,
        window_end: datetime,
    ) -> dict[str, Any]:
        return self._request_json(
            "POST",
            "/v1/lookup",
            read_timeout=self._timeouts.read_s,
            json_body={
                "intent_id": str(intent_id),
                "window_start": _iso_timestamp(window_start),
                "window_end": _iso_timestamp(window_end),
            },
        )


def _order_payload(order: ExecutionOrder) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "symbol": order.symbol,
        "volume": float(order.volume),
    }
    if order.side is not None:
        payload["side"] = order.side
    if order.comment is not None:
        payload["comment"] = order.comment
    if order.deviation is not None:
        payload["deviation"] = order.deviation
    if order.magic is not None:
        payload["magic"] = order.magic
    if order.price is not None:
        payload["price"] = order.price
    if order.sl is not None:
        payload["sl"] = order.sl
    if order.tp is not None:
        payload["tp"] = order.tp
    if order.type is not None:
        payload["type"] = order.type
    if order.type_filling is not None:
        payload["type_filling"] = order.type_filling
    if order.type_time is not None:
        payload["type_time"] = order.type_time
    return payload
