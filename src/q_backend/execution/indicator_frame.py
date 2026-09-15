"""Shared indicator augmentation for forward execution and the chart endpoint.

Re-exports augment_indicator_frame from q_backend.backtesting.indicator_frame.
"""

from q_backend.backtesting.indicator_frame import augment_indicator_frame

__all__ = ["augment_indicator_frame"]
