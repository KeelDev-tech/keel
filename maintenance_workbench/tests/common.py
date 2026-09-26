import copy
from keel_maint.demo import inputs
from keel_maint.snapshot import make_snapshot

def sample():
    config,data,recipe=inputs()
    return make_snapshot(config,data),recipe

def copy_json(value): return copy.deepcopy(value)
