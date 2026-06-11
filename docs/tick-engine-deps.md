# Tick engine dependency spike (WO12)

## Numba verdict: **Viable**

Resolved versions (Windows, Python 3.12):

| Package | Version |
|---------|---------|
| `numpy` | 2.4.4 |
| `numba` | 0.65.1 |

`uv add numba` resolved without downgrading `numpy>=2.4.4` or `pandas>=3.0.2`.

### Smoke test

```python
import numpy
from numba import njit

@njit
def kernel(x):
    return x.sum()

x = numpy.arange(1000, dtype=numpy.float64)
assert kernel(x) == 499500.0
```

**WO13 kernel uses `@njit`.**

## PyArrow

`pyarrow>=24.0.0` added for on-disk tick cache parquet read/write. No NumPy pin conflict.
