"""Bounded contracts shared by the integrated flow tools."""
from datetime import datetime, timezone
import math
from maintenance_workbench.keel_maint.contracts import (
    ContractError, canonical, digest, strict_json, keys, version,
    text, integer, require, hexdigest,
)
from maintenance_workbench.keel_maint.monitoring import timestamp


def number(value, name="number", *, minimum=0, maximum=1e12):
    require(type(value) in (int, float), name + ": finite number required")
    require(minimum <= value <= maximum, name + ": outside permitted range")
    require(math.isfinite(value), name + ": finite number required")
    return value


def boolean(value, name="flag"):
    require(type(value) is bool, name + ": explicit boolean required")
    return value


def records(value, name="records", maximum=10000):
    require(type(value) is list and len(value) <= maximum and all(type(r) is dict for r in value),
            name + ": bounded object array required")
    return value


def clock(now):
    require(isinstance(now, datetime) and now.utcoffset() is not None, "aware evaluation time required")
    return now.astimezone(timezone.utc)


def fresh(stamp, now, max_age=90):
    return 0 <= (clock(now) - timestamp(stamp)).total_seconds() <= max_age


def unique(rows, key):
    seen = set()
    for row in records(rows):
        value = text(row.get(key), key)
        require(value not in seen, "duplicate " + key)
        seen.add(value)
    return rows


def wilson(successes, trials):
    """Two-sided 95% Wilson interval; independence is not established here."""
    integer(successes); integer(trials)
    require(successes <= trials, "successes exceed trials")
    if not trials:
        return None
    z = 1.959963984540054
    p = successes / trials
    denominator = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denominator
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denominator
    return [max(0.0, center - half), min(1.0, center + half)]
