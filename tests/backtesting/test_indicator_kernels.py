"""Tests for q_core integration and indicator kernel bridge."""

from __future__ import annotations

import importlib.metadata
import re


def test_q_core_installed():
    version = importlib.metadata.version("q-core")
    assert version
    import q_core

    rev = q_core.contracts_rev()
    assert isinstance(rev, str)
    assert len(rev) == 40
    assert bool(re.fullmatch(r"[0-9a-f]{40}", rev))


def test_check_kernels_missing_raises_import_error():
    import types
    import pytest
    from q_backend.backtesting.indicator_kernels import REQUIRED_KERNELS, check_kernels

    # Stub missing the first required kernel
    missing_kernel = REQUIRED_KERNELS[0]
    stub_dict = {k: (lambda *args, **kwargs: None) for k in REQUIRED_KERNELS[1:]}
    stub_dict["version"] = lambda: "0.0.0"
    stub = types.SimpleNamespace(**stub_dict)

    with pytest.raises(ImportError) as exc_info:
        check_kernels(stub)

    msg = str(exc_info.value)
    assert missing_kernel in msg
    assert "0.0.0" in msg


def test_as_float64_conversions():
    import numpy as np
    import pandas as pd
    from q_backend.backtesting.indicator_kernels import as_float64

    # Contiguous float64 shares memory
    s_f64 = pd.Series([1.0, 2.0, 3.0], dtype="float64")
    arr_f64 = as_float64(s_f64)
    assert np.shares_memory(s_f64.to_numpy(), arr_f64)

    # int64 converts to [0.0, 1.0, 2.0]
    s_int = pd.Series([0, 1, 2], dtype="int64")
    arr_int = as_float64(s_int)
    assert np.allclose(arr_int, [0.0, 1.0, 2.0])

    # Float64 with pd.NA converts to [1.0, nan]
    s_na = pd.Series([1.0, pd.NA], dtype="Float64")
    arr_na = as_float64(s_na)
    assert arr_na[0] == 1.0
    assert np.isnan(arr_na[1])


def test_shared_index():
    import pandas as pd
    import pytest
    from q_backend.backtesting.indicator_kernels import shared_index

    # Mismatched RangeIndexes raise ValueError
    s1 = pd.Series([1, 2, 3], index=pd.RangeIndex(3))
    s2 = pd.Series([1, 2, 3], index=pd.RangeIndex(1, 4))
    with pytest.raises(ValueError, match="share an identical index"):
        shared_index(s1, s2)

    # Columns of the same frame share an equivalent index
    df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
    col_a = df["a"]
    col_b = df["b"]
    idx = shared_index(col_a, col_b)
    assert idx.equals(df.index)
    assert idx is col_a.index


def test_to_series_shares_memory():
    import numpy as np
    import pandas as pd
    from q_backend.backtesting.indicator_kernels import to_series

    arr = np.array([10.0, 20.0, 30.0], dtype="float64")
    idx = pd.RangeIndex(3)
    s = to_series(arr, idx, "my_series")
    assert s.name == "my_series"
    assert s.index is idx
    assert np.shares_memory(arr, s.to_numpy())


def test_require_window():
    import pytest
    from q_backend.backtesting.indicator_kernels import require_window

    with pytest.raises(ValueError, match=r"window must be >= 1 \(got 0\)\."):
        require_window("window", 0, 1)

    assert require_window("window", 5, 1) == 5


def test_parameter_domain_errors():
    import pandas as pd
    import pytest
    from q_backend.backtesting.technical_indicators import (
        compute_yang_zhang,
        compute_rsi,
        compute_atr,
    )
    from q_backend.backtesting.moving_averages import compute_ma
    from q_backend.backtesting.transforms import compute_rolling_rank

    c = pd.Series([10.0, 11.0, 12.0, 11.5, 12.5], name="close")
    o = c.copy()
    h = c + 1.0
    l = c - 1.0

    # Yang-Zhang with window < 2 raises ValueError
    with pytest.raises(ValueError, match=r"window must be >= 2 \(got 1\)\."):
        compute_yang_zhang(o, h, l, c, window=1)

    # RSI with period < 1 raises ValueError
    with pytest.raises(ValueError, match=r"period must be >= 1 \(got 0\)\."):
        compute_rsi(c, period=0)

    # ATR with period < 1 raises ValueError
    with pytest.raises(ValueError, match=r"period must be >= 1 \(got 0\)\."):
        compute_atr(h, l, c, period=0)

    # Mismatched index on compute_atr raises ValueError
    h_mismatched = h.copy()
    h_mismatched.index = pd.RangeIndex(1, len(h) + 1)
    with pytest.raises(ValueError, match="share an identical index"):
        compute_atr(h_mismatched, l, c, period=14)

    # Rolling rank with window < 1 raises ValueError
    with pytest.raises(ValueError, match=r"window must be >= 1 \(got 0\)\."):
        compute_rolling_rank(c, window=0)

    # MA with period 0 raises existing ValueError
    with pytest.raises(ValueError, match="MA period must be at least 1."):
        compute_ma(c, period=0, ma_type="sma")


def test_indicator_modules_hygiene():
    from pathlib import Path

    backend_src = Path(__file__).resolve().parents[2] / "src" / "q_backend"
    assert backend_src.is_dir()

    # 1. Only the bridge modules import q_core
    q_core_importers = []
    for py_file in backend_src.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        for line in content.splitlines():
            line_str = line.strip()
            if line_str.startswith("import q_core") or line_str.startswith("from q_core"):
                q_core_importers.append(py_file.relative_to(backend_src).as_posix())
                break

    assert sorted(q_core_importers) == [
        "backtesting/indicator_kernels.py",
        "backtesting/tick/kernel.py",
    ]

    # 2. The three indicator modules contain no legacy pandas/numpy arithmetic constructs
    forbidden_tokens = (
        ".rolling(",
        ".ewm(",
        ".shift(",
        ".clip(",
        ".diff(",
        "np.log",
        "np.sqrt",
    )
    indicator_modules = [
        backend_src / "backtesting" / "technical_indicators.py",
        backend_src / "backtesting" / "moving_averages.py",
        backend_src / "backtesting" / "transforms.py",
    ]
    for mod_path in indicator_modules:
        content = mod_path.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            assert token not in content, f"{mod_path.name} contains forbidden token {token!r}"
