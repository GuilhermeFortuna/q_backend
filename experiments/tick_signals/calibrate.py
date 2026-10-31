"""How well inferred flow tracks real aggressor flags, by window length and size bucket."""

import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from features import load

for sym in sys.argv[1:]:
    df = load(sym)
    df = df[df["has_true"]]
    print(sym, "sessions", df.index.normalize().nunique())
    for freq in ("1min", "5min", "30min", "60min"):
        s = df.resample(freq).sum().query("volume > 0")
        print(
            f"  {freq:>6} all {s.signed.corr(s.true_signed):.3f}  large {s.signed_large.corr(s.true_signed_large):.3f}"
            f"  small {s.signed_small.corr(s.true_signed_small):.3f}"
        )
