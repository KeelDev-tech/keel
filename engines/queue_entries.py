"""Shared queue-entry loader (keel engines).

List-shaped queue files pass through unchanged. Dict-shaped queue files
unwrap the entry list from the first present of the keys engines have
historically written ("entries", "items", "leads"); anything else yields [].

Canonical home for this disagreement: feeder_watchdog.load_queue used
"entries"/"items" while buffer_queue_consistency_sweep.load_queue_statuses
used "leads" — one consumer could see entries the other could not.
"""
def load_queue_entries(data):
    """Return the entry list for a queue payload (list or dict)."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("entries", "items", "leads"):
            entries = data.get(key)
            if isinstance(entries, list):
                return entries
    return []
