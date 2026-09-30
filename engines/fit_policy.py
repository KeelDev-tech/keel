"""Read the canonical intake floor; tray overrides are presentation policy."""
import math


def main_floor():
    try:
        from . import queue_intake
    except ImportError:
        import queue_intake
    value = queue_intake.FIT_BAR
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError('canonical intake fit floor is invalid')
    return value
