"""Turn raw trade prints into one-minute order-flow columns, cached per symbol.

Prints sharing a millisecond are merged into one aggressive order. An order's side comes from the
tick rule, so feeds without aggressor flags (WDO$N, WIN$N) work; when real flags exist they are kept
in `true_*` columns for calibration. Every column is additive or a per-minute level, so any clock
window can be built from minutes that closed before a decision.

    python experiments/tick_signals/features.py WDO$N --root data/tick_store
"""

from __future__ import annotations

import argparse
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

CACHE = Path(os.getenv("Q_TICK_SIGNALS_CACHE", "data/experiments/tick_signals"))
FLAG_LAST, FLAG_BUY, FLAG_SELL = 8, 32, 64


@dataclass(frozen=True)
class Spec:
    """Order-size buckets in contracts and the price tick."""

    tick: float
    small: float
    large: float
    huge: float


SPECS = {
    "WDO": Spec(tick=0.5, small=5, large=300, huge=1000),
    "WIN": Spec(tick=5.0, small=5, large=150, huge=500),
    "IND": Spec(tick=5.0, small=1, large=30, huge=100),
}


def spec_for(symbol: str) -> Spec:
    return SPECS[symbol[:3]]


def tick_rule(first: np.ndarray, last: np.ndarray) -> np.ndarray:
    """Side of each order: its own price move, else the move from the prior order, else the prior side."""
    inner = np.sign(last - first)
    between = np.sign(first - np.concatenate(([first[0]], last[:-1])))
    side = np.where(inner != 0, inner, between).astype(float)
    side[side == 0] = np.nan
    return pd.Series(side).ffill().fillna(0.0).to_numpy()


def day_features(path: str, spec: Spec) -> pd.DataFrame | None:
    """One session's minute table, or None when it holds no trades."""
    t = pq.read_table(path, columns=["time_msc", "last", "volume", "flags"]).to_pandas()
    flags = t["flags"].to_numpy()
    keep = (t["last"].to_numpy() > 0) & (t["volume"].to_numpy() > 0) & ((flags & FLAG_LAST) != 0)
    t = t.loc[keep]
    if t.empty:
        return None
    t = t.sort_values("time_msc", kind="stable")
    msc, price, vol = (t[c].to_numpy() for c in ("time_msc", "last", "volume"))
    flags = t["flags"].to_numpy()

    buy, sell = (flags & FLAG_BUY) != 0, (flags & FLAG_SELL) != 0
    sided = buy ^ sell
    has_true = buy.mean() < 0.95 and sided.mean() > 0.5  # a constant flag on every print says nothing
    true_sign = np.where(sided, np.where(buy, 1.0, -1.0), 0.0) if has_true else np.zeros(len(t))

    # Merge prints sharing a millisecond into orders
    starts = np.flatnonzero(np.r_[True, msc[1:] != msc[:-1]])
    ends = np.r_[starts[1:], len(msc)] - 1
    o_vol = np.add.reduceat(vol, starts)
    o_true = np.add.reduceat(true_sign * vol, starts)
    o_hi, o_lo = np.maximum.reduceat(price, starts), np.minimum.reduceat(price, starts)
    side = tick_rule(price[starts], price[ends])
    sweep = (o_hi - o_lo) >= spec.tick
    small, large, huge = o_vol <= spec.small, o_vol >= spec.large, o_vol >= spec.huge

    minute = msc[starts] // 60_000
    frame = pd.DataFrame(
        {
            "minute": minute,
            "n_orders": 1.0,
            "signed": side * o_vol,
            "signed_small": side * o_vol * small,
            "vol_small": o_vol * small,
            "signed_large": side * o_vol * large,
            "vol_large": o_vol * large,
            "n_large": large.astype(float),
            "signed_huge": side * o_vol * huge,
            "vol_huge": o_vol * huge,
            "signed_sweep": side * o_vol * sweep,
            "vol_sweep": o_vol * sweep,
            "true_signed": o_true,
            "true_signed_large": o_true * large,
            "true_signed_small": o_true * small,
        }
    )
    out = frame.groupby("minute").sum()

    prints = pd.DataFrame({"minute": msc // 60_000, "p": price, "v": vol})
    prints["path"] = np.abs(np.diff(price, prepend=price[0]))
    grouped = prints.groupby("minute")
    out["open"], out["high"], out["low"], out["close"] = (
        grouped["p"].first(),
        grouped["p"].max(),
        grouped["p"].min(),
        grouped["p"].last(),
    )
    out["volume"], out["path"], out["n_prints"] = grouped["v"].sum(), grouped["path"].sum(), grouped["p"].size()
    out["has_true"] = has_true
    out.index = pd.to_datetime(out.index, unit="m")
    out.index.name = "time"
    return out


def _worker(args: tuple[str, Spec]) -> pd.DataFrame | None:
    return day_features(*args)


def build(symbol: str, root: Path, workers: int = 6, force: bool = False) -> Path:
    """Build or reuse the symbol's minute table."""
    out = CACHE / f"{symbol}.parquet"
    if out.exists() and not force:
        return out
    paths = sorted(str(p) for p in (root / symbol).glob("*.parquet"))
    spec = spec_for(symbol)
    with ProcessPoolExecutor(workers) as pool:
        days = [d for d in pool.map(_worker, [(p, spec) for p in paths], chunksize=4) if d is not None]
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.concat(days).to_parquet(out, compression="zstd")
    return out


def load(symbol: str) -> pd.DataFrame:
    """Minute table restricted to the regular session, without partial days."""
    df = pd.read_parquet(CACHE / f"{symbol}.parquet")
    df = df[(df.index.time >= pd.Timestamp("09:00").time()) & (df.index.time < pd.Timestamp("18:30").time())]
    session = df.index.normalize()
    prints = df["n_prints"].groupby(session).transform("sum")
    return df[prints >= 0.2 * prints.groupby(session).first().median()]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+")
    parser.add_argument("--root", type=Path, default=Path("data/tick_store"))
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    for sym in args.symbols:
        print(sym, build(sym, args.root, args.workers, args.force), flush=True)
