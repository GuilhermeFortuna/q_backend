"""Causal market gateway, exercised through the real RemoteMt5Client over loopback HTTP."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pytest

from q_backend.market_data.clients.remote import RemoteMt5Client
from q_backend.replay.dataset import load_manifest, verify_manifest
from q_backend.replay.gateway import Ingress, ServerThread, create_gateway_app
from q_backend.replay.market import ReplayMarket, load_market, utc_to_wire_ms

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
SPEC = {
    "description": "mini index future",
    "point": 1.0,
    "digits": 0,
    "volume_min": 1.0,
    "volume_step": 1.0,
    "volume_max": 100.0,
    "trade_contract_size": 1.0,
    "margin_per_contract": "1000.00",
}
TRADE = 8 | 16 | 32  # LAST | VOLUME | BUY
QUOTE = 2 | 4  # BID | ASK
BASE = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)  # 09:00 in Brasília (UTC-3, no DST in 2026)


_EPOCH_MS = int(BASE.timestamp() * 1000)


def _ms(seconds: float) -> int:
    """UTC milliseconds of BASE plus the given offset in seconds."""
    return _EPOCH_MS + int(seconds * 1000)


def _write_dataset(root: Path, symbol: str, ticks: list[tuple], coverage: tuple[datetime, datetime]) -> Path:
    """Freeze ticks as the tick NPZ format and write a manifest referencing it."""
    rows = sorted(ticks, key=lambda row: row[0])  # stable: equal times keep their input order
    columns = {
        "time_msc": np.array([row[0] for row in rows], dtype=np.int64),
        "bid": np.array([row[1] for row in rows], dtype=np.float64),
        "ask": np.array([row[2] for row in rows], dtype=np.float64),
        "last": np.array([row[3] for row in rows], dtype=np.float64),
        "volume": np.array([row[4] for row in rows], dtype=np.float64),
        "flags": np.array([row[5] for row in rows], dtype=np.int32),
    }
    tick_path = root / f"{symbol}.npz"
    np.savez(tick_path, **columns)
    content = tick_path.read_bytes()
    manifest = {
        "schema_version": 1,
        "time_unit": "msc",
        "exchange_timezone": "America/Sao_Paulo",
        "coverage_start_utc": coverage[0].isoformat(),
        "coverage_end_utc": coverage[1].isoformat(),
        "files": [
            {
                "path": tick_path.name,
                "symbol": symbol,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        ],
        "symbol_specs": {symbol: SPEC},
    }
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


@dataclass
class Gateway:
    client: RemoteMt5Client
    market: ReplayMarket
    server: ServerThread
    visible: dict


@pytest.fixture
def gateway_factory(tmp_path: Path):
    servers: list[ServerThread] = []

    def build(
        ticks, *, visible_utc: datetime, token: str | None = None, symbol="WIN$N", coverage=None, zone=SAO_PAULO
    ) -> Gateway:
        coverage = coverage or (BASE - timedelta(hours=1), BASE + timedelta(hours=2))
        path = _write_dataset(tmp_path, symbol, ticks, coverage)
        manifest = load_manifest(path)
        verify_manifest(manifest, tmp_path, symbols=[symbol], start_utc=coverage[0], end_utc=coverage[1])
        market = load_market(manifest, tmp_path, symbols=[symbol], generation="gen-1", zone=zone)
        visible = {"us": int(visible_utc.timestamp() * 1_000_000)}
        app = create_gateway_app(
            market=market,
            ingress=lambda: Ingress(visible_us=visible["us"], clock={"sequence": 1}),
            token=token,
        )
        server = ServerThread(app)
        servers.append(server)
        url = server.start()
        client = RemoteMt5Client(base_url=url, token=token, timeout=5.0, history_timeout=5.0)
        return Gateway(client=client, market=market, server=server, visible=visible)

    yield build
    for server in servers:
        server.stop()


def _wire(utc_ms: int) -> int:
    """Brasília wall clock encoded as UTC milliseconds, as the MT5 wire does."""
    return utc_ms - 3 * 3_600_000


def _standard_ticks() -> list[tuple]:
    t0 = _ms(0)
    t10 = _ms(10)
    t70 = _ms(70)
    t130 = _ms(130)
    return [
        (t0, 100.0, 101.0, 100.5, 2, TRADE),
        (t10, 99.5, 101.5, 0.0, 0, QUOTE),  # quote-only update: no trade price
        (t10, 100.8, 101.2, 101.0, 3, TRADE),  # duplicate timestamp, kept
        (t10, 100.9, 101.7, 101.5, 1, TRADE),  # second duplicate, occurrence 1
        (t70, 98.5, 99.5, 99.0, 1, TRADE),
        (t130, 101.5, 102.5, 102.0, 5, TRADE),
    ]


def test_health_contract(gateway_factory) -> None:
    gw = gateway_factory(_standard_ticks(), visible_utc=BASE + timedelta(seconds=200))
    assert gw.client.connect() is True
    assert gw.client.is_available() is True


def test_ticks_are_causal_and_keep_duplicates_in_wire_time(gateway_factory) -> None:
    gw = gateway_factory(_standard_ticks(), visible_utc=BASE + timedelta(seconds=75))
    columns = gw.client.get_ticks_columnar(
        "WIN$N", datetime(2026, 10, 1, 8, 0), datetime(2026, 10, 1, 10, 0), use_cache=False
    )
    # Ticks at 130 s are not visible at 75 s; both duplicates at 10 s are returned.
    assert len(columns["time_msc"]) == 5
    assert list(columns["time_msc"]) == [
        _wire(_ms(0)),
        _wire(_ms(10)),
        _wire(_ms(10)),
        _wire(_ms(10)),
        _wire(_ms(70)),
    ]
    assert columns["time_msc"][0] == int(datetime(2026, 10, 1, 9, 0, tzinfo=UTC).timestamp() * 1000)


def test_range_end_is_exclusive_and_future_is_never_revealed(gateway_factory) -> None:
    gw = gateway_factory(_standard_ticks(), visible_utc=BASE + timedelta(seconds=200))
    columns = gw.client.get_ticks_columnar(
        "WIN$N", datetime(2026, 10, 1, 9, 0), datetime(2026, 10, 1, 9, 0, 10), use_cache=False
    )
    assert len(columns["time_msc"]) == 1  # the 10 s group starts exactly at the exclusive end


def test_trade_only_flag_drops_quote_updates(gateway_factory) -> None:
    gw = gateway_factory(_standard_ticks(), visible_utc=BASE + timedelta(seconds=200))
    everything = gw.client.get_ticks_columnar(
        "WIN$N", datetime(2026, 10, 1, 8, 0), datetime(2026, 10, 1, 10, 0), use_cache=False
    )
    trades = gw.client.get_ticks_columnar(
        "WIN$N", datetime(2026, 10, 1, 8, 0), datetime(2026, 10, 1, 10, 0), flags=2, use_cache=False
    )
    assert len(everything["time_msc"]) == 6
    assert len(trades["time_msc"]) == 5


def test_ohlcv_is_causal_with_forming_bar(gateway_factory) -> None:
    gw = gateway_factory(_standard_ticks(), visible_utc=BASE + timedelta(seconds=75))
    bars = gw.client.get_ohlcv("WIN$N", "M1", datetime(2026, 10, 1, 8, 0), datetime(2026, 10, 1, 10, 0))
    assert [bar.time.isoformat() for bar in bars] == ["2026-10-01T09:00:00", "2026-10-01T09:01:00"]
    first = bars[0]
    # Quote-only rows are not trade prices; three trade prices form the 09:00 bar.
    assert (first.open, first.high, first.low, first.close) == (100.5, 101.5, 100.5, 101.5)
    assert first.tick_volume == 3
    assert first.real_volume == 6  # trade-flagged volume 2 + 3 + 1 (the duplicate at 10 s is a trade)
    forming = bars[1]
    assert forming.close == 99.0  # the 09:01 bar is still forming at 75 s, with ticks up to 70 s


def test_completed_bar_appears_only_after_its_boundary(gateway_factory) -> None:
    gw = gateway_factory(_standard_ticks(), visible_utc=BASE + timedelta(seconds=75))
    early = gw.client.get_ohlcv("WIN$N", "M1", datetime(2026, 10, 1, 8, 0), datetime(2026, 10, 1, 10, 0))
    assert early[-1].time.isoformat() == "2026-10-01T09:01:00"
    gw.visible["us"] = int((BASE + timedelta(seconds=130)).timestamp() * 1_000_000)
    later = gw.client.get_ohlcv("WIN$N", "M1", datetime(2026, 10, 1, 8, 0), datetime(2026, 10, 1, 10, 0))
    assert [bar.time.isoformat() for bar in later] == [
        "2026-10-01T09:00:00",
        "2026-10-01T09:01:00",
        "2026-10-01T09:02:00",
    ]
    assert later[-1].close == 102.0


def test_available_range_stops_at_the_visibility_watermark(gateway_factory) -> None:
    gw = gateway_factory(_standard_ticks(), visible_utc=BASE + timedelta(seconds=75))
    found = gw.client.get_available_ohlcv_range("WIN$N", "M1")
    assert found is not None
    assert found.end.isoformat() == "2026-10-01T09:01:00"
    assert found.bar_count == 2


def test_tick_pages_are_lossless_across_duplicate_groups(gateway_factory) -> None:
    gw = gateway_factory(_standard_ticks(), visible_utc=BASE + timedelta(seconds=200))
    start = _wire(_ms(-3600))
    end = _wire(_ms(3600))
    collected: list[int] = []
    cursor = start
    pages = 0
    while True:
        page = gw.client.get_tick_page("WIN$N", cursor, end, limit=2)
        collected.extend(int(t) for t in page.columns["time_msc"])
        pages += 1
        if page.exhausted:
            break
        cursor = page.next_cursor_msc
    assert pages >= 3
    assert collected == [_wire(_ms(0))] + [_wire(_ms(10))] * 3 + [_wire(_ms(70)), _wire(_ms(130))]


def test_trades_endpoint_uses_true_utc_and_occurrence_indices(gateway_factory) -> None:
    gw = gateway_factory(_standard_ticks(), visible_utc=BASE + timedelta(seconds=200))
    trade_range = gw.client.get_trades("WIN$N", BASE - timedelta(hours=1), BASE + timedelta(seconds=150))
    assert trade_range.availability == "available"
    assert trade_range.range_complete is True
    assert list(trade_range.columns["time_msc"]) == [_ms(0), _ms(10), _ms(10), _ms(70), _ms(130)]
    assert list(trade_range.columns["occurrence"]) == [0, 0, 1, 0, 0]
    assert list(trade_range.columns["price"]) == [100.5, 101.0, 101.5, 99.0, 102.0]


def test_trades_range_past_visibility_is_incomplete(gateway_factory) -> None:
    gw = gateway_factory(_standard_ticks(), visible_utc=BASE + timedelta(seconds=75))
    trade_range = gw.client.get_trades("WIN$N", BASE - timedelta(hours=1), BASE + timedelta(hours=1))
    assert trade_range.range_complete is False
    assert trade_range.coverage_reason == "range_not_yet_visible"
    assert max(trade_range.columns["time_msc"]) <= _ms(75)


def test_unknown_symbol_maps_to_client_empty_results(gateway_factory) -> None:
    gw = gateway_factory(_standard_ticks(), visible_utc=BASE + timedelta(seconds=75))
    assert gw.client.get_symbol_info("NOPE") is None
    assert gw.client.get_available_ohlcv_range("NOPE", "M1") is None
    assert gw.client.get_ohlcv("NOPE", "M1", datetime(2026, 10, 1, 8), datetime(2026, 10, 1, 10)) == []


def test_token_is_required_when_configured(gateway_factory) -> None:
    gw = gateway_factory(_standard_ticks(), visible_utc=BASE, token="s3cret")
    assert gw.client.is_available() is True
    with httpx.Client(base_url=gw.client._base_url) as anonymous:
        assert anonymous.get("/v1/health").status_code == 401
    wrong = RemoteMt5Client(base_url=gw.client._base_url, token="wrong", timeout=5.0)
    assert wrong.is_available() is False


def test_invalid_timeframe_and_naive_trade_times_are_rejected(gateway_factory) -> None:
    gw = gateway_factory(_standard_ticks(), visible_utc=BASE)
    with httpx.Client(base_url=gw.client._base_url) as raw:
        bad_tf = raw.get(
            "/v1/ohlcv",
            params={
                "symbol": "WIN$N",
                "timeframe": "M7",
                "start": "2026-10-01T08:00:00",
                "end": "2026-10-01T09:00:00",
            },
        )
        assert bad_tf.status_code == 400 and bad_tf.json()["code"] == "invalid_timeframe"
        naive_trades = raw.get(
            "/v1/trades",
            params={
                "symbol": "WIN$N",
                "start_utc": "2026-10-01T08:00:00",
                "end_utc": "2026-10-01T09:00:00",
            },
        )
        assert naive_trades.status_code == 400 and naive_trades.json()["code"] == "invalid_datetime"


def test_historical_offset_applies_at_the_tick_instant() -> None:
    """Brasília observed DST until 2019: the wire wall clock follows the offset of that instant."""
    instant = datetime(2018, 11, 4, 12, 0, tzinfo=UTC)
    summer = utc_to_wire_ms(np.array([int(instant.timestamp() * 1000)], dtype=np.int64), SAO_PAULO)
    winter = utc_to_wire_ms(
        np.array([int(datetime(2018, 7, 4, 12, 0, tzinfo=UTC).timestamp() * 1000)], dtype=np.int64), SAO_PAULO
    )
    assert int(summer[0]) == int(datetime(2018, 11, 4, 10, 0, tzinfo=UTC).timestamp() * 1000)
    assert int(winter[0]) == int(datetime(2018, 7, 4, 9, 0, tzinfo=UTC).timestamp() * 1000)


def test_dataset_checksum_mutation_blocks_the_gateway(tmp_path: Path) -> None:
    coverage = (BASE - timedelta(hours=1), BASE + timedelta(hours=2))
    path = _write_dataset(tmp_path, "WIN$N", _standard_ticks(), coverage)
    manifest = load_manifest(path)
    (tmp_path / "WIN$N.npz").write_bytes(b"not the frozen file")
    from q_backend.replay.dataset import DatasetError

    with pytest.raises(DatasetError):
        verify_manifest(manifest, tmp_path, symbols=["WIN$N"], start_utc=coverage[0], end_utc=coverage[1])
