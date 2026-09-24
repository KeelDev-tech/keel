"""ats_discovery — keyless ATS discovery adapters for the Western Application Pipeline.

Adapters fetch employer career boards over public read-only endpoints and
emit normalized candidate dicts. They never write to queues or the ledger;
sweep.py stages them via the sanctioned staging-ingest path.

Platforms (wiring order per 2026-09-15 validation):
  smartrecruiters, pinpoint, rippling, workable,
  recruitee, breezy, bamboohr (content-type guard), personio (opt-in guards)

Modules are imported defensively: a missing/broken adapter module never
breaks the package import — it is simply absent from ADAPTERS.
"""

import importlib

ADAPTERS = {}
for _name in ("smartrecruiters", "pinpoint", "rippling", "workable",
              "recruitee", "breezy", "bamboohr", "personio"):
    try:
        ADAPTERS[_name] = importlib.import_module(
            f".{_name}", __name__)
    except Exception:
        continue
del _name

# Expose loaded modules as attributes for `ats_discovery.smartrecruiters` style.
globals().update(ADAPTERS)

# Wiring order (validation-ranked). Marginal platforms stay last and guarded.
WIRING_ORDER = [
    "smartrecruiters", "pinpoint", "rippling", "workable",
    "recruitee", "breezy", "bamboohr", "personio",
]
