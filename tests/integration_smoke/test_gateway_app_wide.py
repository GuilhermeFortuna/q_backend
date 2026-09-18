"""Gateway app-wide validation smoke (WO190).

Default ``uv run pytest`` runs only the **fake-gateway** cases: an in-process
``mt5_gateway`` HTTP server backed by a controllable fake ``MetaTrader5`` module.
They never contact a developer's live Wine gateway (the autouse conftest still
clears ``Q_MT5_GATEWAY_URL``; these tests set their own URL on the ephemeral port).

Live pass (opt-in) — requires a reachable gateway at ``Q_MT5_GATEWAY_URL``::

    Q_MT5_GATEWAY_URL=http://127.0.0.1:18812 \\
      uv run pytest tests/integration_smoke/test_gateway_app_wide.py -m live_gateway -v

Manual UI checklist: ``q_frontend/docs/dev/gateway-app-wide-validation.md``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from urllib.request import urlopen

import numpy as np
import pytest

from q_backend.market_data import local_store
from q_backend.market_data.models import OHLCV
from q_backend.market_data.read_through import read_ohlcv_fresh
from q_backend.market_data.service import MarketDataService
from q_backend.storage.runtime_config import set_data_source

from tests.gateway.conftest import GATEWAY_PATH, load_module, running_gateway_server
from tests.gateway.fake_metatrader5 import make_fake_mt5
from tests.gateway.test_mt5_gateway import _RATE_DTYPE

_SYMBOL = "WIN$"
_TIMEFRAME = "M5"
_STALE_THROUGH = datetime(2024, 1, 5, 18, 0)
_REQUEST_END = datetime(2024, 1, 8, 18, 0)


def _live_gateway_url() -> str | None:
    import os

    url = (os.environ.get("Q_MT5_GATEWAY_URL") or "").strip()
    return url or None


def _gateway_reachable(url: str) -> bool:
    try:
        with urlopen(f"{url.rstrip('/')}/v1/health", timeout=2) as resp:
            payload = json.loads(resp.read())
        return payload.get("status") == "ok"
    except Exception:
        return False


def pytest_configure(config):
    import os

    config._wo190_live_gateway_url = (os.environ.get("Q_MT5_GATEWAY_URL") or "").strip() or None


live_gateway = pytest.mark.live_gateway


def _require_live_gateway(request) -> str:
    url = getattr(request.config, "_wo190_live_gateway_url", None)
    if not url:
        pytest.skip("Q_MT5_GATEWAY_URL not set (pass env for live gateway pass)")
    if not _gateway_reachable(url):
        pytest.skip(f"gateway unreachable at {url}")
    return url


def _m5_bar(when: datetime, close: float = 130_000.0) -> OHLCV:
    return OHLCV(
        time=when,
        open=close,
        high=close + 50.0,
        low=close - 50.0,
        close=close,
        tick_volume=100,
    )


def _seed_stale_local() -> None:
    bars = [_m5_bar(_STALE_THROUGH - timedelta(days=offset)) for offset in (4, 3, 2, 1, 0)]
    local_store.write_ohlcv(_SYMBOL, _TIMEFRAME, bars)


def _rates_for_range(start: datetime, end: datetime) -> np.ndarray:
    rows = []
    cursor = start + timedelta(days=1)
    close = 131_000.0
    while cursor <= end:
        epoch = int(cursor.replace(tzinfo=None).timestamp())
        rows.append((epoch, close, close + 10, close - 10, close, 200, 1, 0))
        close += 10.0
        cursor += timedelta(days=1)
    return np.array(rows, dtype=_RATE_DTYPE)


def _install_counting_rates(fake_mt5) -> list[tuple]:
    calls: list[tuple] = []
    state = fake_mt5._state

    def copy_rates_range(symbol, timeframe, date_from, date_to):
        calls.append((symbol, timeframe, date_from, date_to))
        return _rates_for_range(date_from, date_to)

    fake_mt5.copy_rates_range = copy_rates_range
    state["rates_calls"] = calls
    return calls


@pytest.fixture
def gateway_market_root(tmp_path, monkeypatch):
    root = tmp_path / "market"
    monkeypatch.setenv("Q_MARKET_DATA_ROOT", str(root))
    monkeypatch.setenv("Q_RUNTIME_CONFIG_PATH", str(tmp_path / "runtime_config.json"))
    monkeypatch.delenv("Q_MT5_GATEWAY_TOKEN", raising=False)
    set_data_source("auto")
    yield root


@pytest.fixture
def fake_http_gateway(gateway_market_root, monkeypatch):
    """In-process gateway + RemoteMt5Client wired through a real MarketDataService."""
    fake_mt5 = make_fake_mt5()
    fake_mt5._state["known_symbols"].add(_SYMBOL)
    fake_mt5._state["symbol_info"][_SYMBOL] = fake_mt5._state["symbol_info"]["WIN$"]
    rates_calls = _install_counting_rates(fake_mt5)
    gateway = load_module(GATEWAY_PATH, "gateway_app_wide_gateway", fake_mt5)

    with running_gateway_server(gateway) as base_url:
        monkeypatch.setenv("Q_MT5_GATEWAY_URL", base_url)
        service = MarketDataService()
        monkeypatch.setattr(
            "q_backend.market_data.routing.symbol_selectable_in_mt5",
            lambda _svc, _symbol: False,
        )
        yield service, rates_calls


def _request_start() -> datetime:
    return _STALE_THROUGH - timedelta(days=3)


def test_gap_fill_extends_envelope_and_second_read_hits_gateway_once(
    fake_http_gateway,
):
    service, rates_calls = fake_http_gateway
    _seed_stale_local()
    start = _request_start()

    first = service.get_ohlcv(_SYMBOL, _TIMEFRAME, start, _REQUEST_END)
    assert len(first) > len([_m5_bar(_STALE_THROUGH)])
    assert rates_calls, "expected one gateway OHLCV fetch for the tail gap"
    first_call_count = len(rates_calls)

    envelope = local_store.available_range(_SYMBOL, _TIMEFRAME)
    assert envelope is not None
    assert envelope.end >= _REQUEST_END.replace(hour=envelope.end.hour, minute=envelope.end.minute)

    second = service.get_ohlcv(_SYMBOL, _TIMEFRAME, start, _REQUEST_END)
    assert len(second) == len(first)
    assert len(rates_calls) == first_call_count, "second identical read must not hit the gateway again"


def test_read_ohlcv_fresh_matches_service_freshness(fake_http_gateway):
    service, _rates_calls = fake_http_gateway
    _seed_stale_local()
    start = _request_start()

    stale_only = local_store.read_ohlcv(_SYMBOL, _TIMEFRAME, start, _REQUEST_END)
    via_service = service.get_ohlcv(_SYMBOL, _TIMEFRAME, start, _REQUEST_END)
    fresh = read_ohlcv_fresh(_SYMBOL, _TIMEFRAME, start, _REQUEST_END, service=service)

    assert len(via_service) > len(stale_only)
    assert [bar.time for bar in fresh] == [bar.time for bar in via_service]


def test_offline_covered_range_still_served(fake_http_gateway, monkeypatch):
    service, _rates_calls = fake_http_gateway
    _seed_stale_local()
    start = _request_start()
    service.get_ohlcv(_SYMBOL, _TIMEFRAME, start, _REQUEST_END)

    monkeypatch.setattr(service._remote_client, "is_available", lambda: False)
    offline = service.get_ohlcv(_SYMBOL, _TIMEFRAME, start, _REQUEST_END)
    assert offline, "covered range must still load from local parquet when gateway is down"


def test_offline_uncovered_range_raises_connection_error(gateway_market_root, monkeypatch):
    _seed_stale_local()

    class _DownRemote:
        def is_supported(self) -> bool:
            return True

        def is_available(self) -> bool:
            return True

        def get_ohlcv(self, *_args, **_kwargs):
            raise ConnectionError("gateway down")

    service = MarketDataService()
    service._remote_client = _DownRemote()
    set_data_source("remote")

    with pytest.raises(ConnectionError, match="gateway down"):
        service.get_ohlcv(
            _SYMBOL,
            _TIMEFRAME,
            _request_start(),
            _REQUEST_END,
        )


def test_read_ohlcv_fresh_offline_uncovered_falls_back_to_local(gateway_market_root, monkeypatch, caplog):
    _seed_stale_local()
    start = _request_start()

    class _FailingService:
        def get_ohlcv(self, *_args, **_kwargs):
            raise ConnectionError("gateway down")

    with caplog.at_level("WARNING"):
        bars = read_ohlcv_fresh(
            _SYMBOL,
            _TIMEFRAME,
            start,
            _STALE_THROUGH,
            service=_FailingService(),  # type: ignore[arg-type]
        )

    assert bars
    assert "falling back to local parquet" in caplog.text


@live_gateway
def test_live_gateway_health_reports_ok(request):
    url = _require_live_gateway(request)
    with urlopen(f"{url.rstrip('/')}/v1/health", timeout=3) as resp:
        payload = json.loads(resp.read())
    assert payload["status"] == "ok"
    assert str(payload["schema_version"]).startswith("1.")


@live_gateway
def test_live_gap_fill_on_temp_store(request, gateway_market_root, monkeypatch):
    """Minimal live scripted check: tail gap fetch + local persist on a temp store."""
    url = _require_live_gateway(request)
    monkeypatch.setenv("Q_MT5_GATEWAY_URL", url)
    _seed_stale_local()
    service = MarketDataService()
    monkeypatch.setattr(
        "q_backend.market_data.routing.symbol_selectable_in_mt5",
        lambda _svc, _symbol: False,
    )
    if not service._remote_client.is_available():
        pytest.skip("configured gateway is not available to RemoteMt5Client")

    start = _request_start()
    before = local_store.available_range(_SYMBOL, _TIMEFRAME)
    assert before is not None

    bars = service.get_ohlcv(_SYMBOL, _TIMEFRAME, start, _REQUEST_END)
    after = local_store.available_range(_SYMBOL, _TIMEFRAME)
    assert after is not None
    assert bars
    assert after.end >= before.end
