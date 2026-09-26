"""Private, host-owned integration configuration. No remote identity provider."""
import os
from pathlib import Path
import stat

from keel_agent.io import read_json
from keel_agent.revisions import PREAPPROVAL_COMPONENTS, _token
from keel_trust.common import keys, require


def load_config(path):
    path = Path(path).absolute()
    require(path.resolve(strict=True) == path, "host configuration cannot use symlinks")
    before = path.stat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
            and before.st_uid == os.getuid() and not stat.S_IMODE(before.st_mode) & 0o077,
            "host configuration must be owned by this operator with private permissions")
    value = read_json(path)
    after = path.stat()
    require((before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns) ==
            (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns),
            "host configuration changed during read")
    keys(value, {"schema", "home", "workspace_id", "action", "producer_components",
                 "attachment_source_root", "operator"})
    require(value["schema"] == "keel.live_host.v1", "unsupported host configuration")
    require(value["action"] == "PREPARE", "this release supports PREPARE only")
    _token(value["workspace_id"])
    for key in ("home", "attachment_source_root"):
        require(type(value[key]) is str and Path(value[key]).is_absolute(),
                "host filesystem roots must be absolute paths")
    producers = value["producer_components"]
    require(type(producers) is dict and 0 < len(producers) <= 100, "bounded producer map required")
    for name, components in producers.items():
        _token(name)
        require(type(components) is list and bool(components)
                and all(type(x) is str and x in PREAPPROVAL_COMPONENTS for x in components)
                and len(components) == len(set(components)), "invalid producer component allowlist")
    from .review import Principal
    operator = value["operator"]
    keys(operator, {"actor_id", "authority_record_ref", "allowed_actions", "allowed_scopes"})
    require(type(operator["allowed_actions"]) is list and
            len(set(operator["allowed_actions"])) == len(operator["allowed_actions"]),
            "unique operator actions required")
    require(type(operator["allowed_scopes"]) is list and len(operator["allowed_scopes"]) <= 2000,
            "bounded operator scopes required")
    Principal(actor_id=operator["actor_id"], authority_record_ref=operator["authority_record_ref"],
              workspace_id=value["workspace_id"], allowed_actions=frozenset(operator["allowed_actions"]),
              allowed_scopes=tuple(operator["allowed_scopes"]))
    return value


def principal_from_config(value):
    from .review import Principal
    operator = value["operator"]
    return Principal(actor_id=operator["actor_id"], authority_record_ref=operator["authority_record_ref"],
                     workspace_id=value["workspace_id"], allowed_actions=frozenset(operator["allowed_actions"]),
                     allowed_scopes=tuple(operator["allowed_scopes"]))
