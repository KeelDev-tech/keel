#!/usr/bin/env python3
"""Canonical queue-status matchers (ARM 75-2, 2026-09-15).

Single source of truth for status predicates shared by the pipeline's
readers. Provenance: ``is_inflight_status`` was first defined in
``engines/application-executor/feeder_watchdog.py`` lines 183-191
(2026-09-15: the authorized two-lane test marks entries IN-FLIGHT-LANE-A
/ IN-FLIGHT-LANE-B, and both count as in-flight so the feeder never
mistakes a live lane for an idle one). feeder_watchdog.py still carries
its own local copy (ARM 75-2 was barred from editing it); this module is
the canonical home every migrated reader points at.

The engines dir has no package __init__, so readers import via:

    import os, sys
    sys.path.insert(0, "<keel-home>/engines")  # or wherever this repo lives
    from status_matchers import is_inflight_status
"""
from __future__ import annotations


def is_inflight_status(status):
    """True for plain IN-FLIGHT and two-lane markers IN-FLIGHT-LANE-A/B.

    Case-insensitive; None-safe. The trailing dash in the prefix check is
    deliberate: ``IN-FLIGHT-LANEX`` does NOT match.
    """
    s = (status or "").upper()
    return s == "IN-FLIGHT" or s.startswith("IN-FLIGHT-LANE-")
