"""Shared bounded data and private-output helpers; no authentication claims."""
import os
from pathlib import Path

from keel_loki.common import (LokiError, atomic_json, canonical, clone, decode_json,
                              digest, load_json, require_dict, require_hash,
                              require_id, require_int)


class MuseError(LokiError):
    pass


FLAGS = {'execution_authorized': False, 'production_deployed': False,
         'account_muse_integration': 'NOT_VERIFIED', 'actual_native_browser': 'NOT_RUN'}


def new_home(path):
    """Create exactly one private directory through an already held parent."""
    from tools.bench_inventory import directory_fd, safe_name
    path = Path(path).absolute()
    safe_name(path.name)
    parent = directory_fd(path.parent)
    try:
        os.mkdir(Path('/proc/self/fd') / str(parent) / path.name, 0o700)
    finally:
        os.close(parent)
    return path


def outside_source(path, root):
    path, root = Path(path).absolute(), Path(root).resolve(strict=True)
    resolved = path.resolve(strict=False)
    if resolved == root or root in resolved.parents:
        raise MuseError('runtime_output_inside_source')
    if path.parent.resolve(strict=True) != path.parent:
        raise MuseError('canonical_existing_parent_required')
    return path
