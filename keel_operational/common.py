"""Shared private JSON helpers and non-authorizing release boundaries."""
from keel_muse.common import (MuseError, atomic_json, canonical, clone,
    decode_json, digest, load_json, new_home, outside_source, require_dict,
    require_hash, require_id, require_int)


class OperationalError(MuseError):
    pass


FLAGS = {
    'execution_authorized': False,
    'production_deployed': False,
    'account_muse_integration': 'NOT_VERIFIED',
    'actual_native_browser': 'NOT_RUN',
    'live_repository_integration': 'NOT_VERIFIED',
    'live_canonical_integration': 'NOT_VERIFIED',
    'real_local_model_inference': 'NOT_RUN',
}
