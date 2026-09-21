"""Strict inputs and immutable bundle construction; no approval issuance."""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


class ContractError(ValueError):
    pass


def text(value, name="value"):
    if type(value) is not str or not value.strip():
        raise ContractError(f"{name}: nonempty string required")
    return value


def number(value, name="value", minimum=0, maximum=None):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ContractError(f"{name}: finite number required")
    if value < minimum or (maximum is not None and value > maximum):
        raise ContractError(f"{name}: outside bounds")
    return value


def integer(value, name="value", minimum=0):
    if type(value) is not int or value < minimum:
        raise ContractError(f"{name}: integer >= {minimum} required")
    return value


def timestamp(value):
    try:
        result = datetime.fromisoformat(text(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("invalid timestamp") from exc
    if result.utcoffset() is None:
        raise ContractError("timestamp must include an offset")
    return result


def utcnow():
    return datetime.now(timezone.utc)


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise ContractError("not canonical JSON") from exc


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError("duplicate JSON key")
            result[key] = value
        return result
    def reject(value):
        raise ContractError("non-finite JSON number")
    result = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)
    canonical(result)  # catches overflow such as 1e999
    return result


def versioned(value):
    if type(value) is not dict or type(value.get("schema_version")) is not int:
        raise ContractError("integer schema_version required")
    if value["schema_version"] != 1:
        raise ContractError("unsupported schema_version")
    return value


def application_identity(candidate_id, provider, employer_id, posting_id):
    """IDs come from verified posting metadata, never title or queue role ID.

    Resolving cross-provider mirrors requires a reviewed alias in the live store.
    This helper does not infer aliases from matching titles.
    """
    parts = [text(x) for x in (candidate_id, provider, employer_id, posting_id)]
    return digest({"v": 1, "candidate": parts[0], "provider": parts[1].lower(),
                   "employer": parts[2], "posting": parts[3]})


def public_url_shape(value):
    try:
        p = urlsplit(text(value))
        return (p.scheme == "https" and bool(p.hostname) and p.port in (None, 443)
                and not p.username and not p.password
                and not any(ord(c) < 33 for c in value) and "\\" not in value)
    except ValueError:
        return False


def atomic_bytes(path, data):
    """Durable replacement on a local POSIX filesystem; not a multi-file tx."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".keel-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def freeze_asset(source, store, media_type):
    data = Path(source).read_bytes()
    if len(data) > 25 * 1024 * 1024:
        raise ContractError("asset exceeds 25 MiB")
    sha = hashlib.sha256(data).hexdigest()
    target = Path(store) / sha
    if target.exists():
        if target.read_bytes() != data:
            raise ContractError("content-addressed asset was modified")
    else:
        atomic_bytes(target, data)
    return {"sha256": sha, "size": len(data), "media_type": text(media_type)}


def approved_bytes(store, asset):
    sha = asset.get("sha256")
    if type(sha) is not str or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ContractError("invalid asset digest")
    data = (Path(store) / sha).read_bytes()
    if len(data) != integer(asset.get("size")) or hashlib.sha256(data).hexdigest() != sha:
        raise ContractError("asset integrity failure")
    return data  # dispatch must upload these bytes, not reopen the original path


def submission_bundle(identity, target, answers, form_version, policy_version, assets):
    """Content fingerprint only. A caller cannot turn this into an approval."""
    for key in ("provider", "employer_id", "posting_id", "url"):
        text(target.get(key), key)
    if not public_url_shape(target["url"]):
        raise ContractError("invalid target URL")
    if type(answers) is not dict or type(assets) is not list:
        raise ContractError("answers and assets have invalid types")
    bundle = {"schema_version": 1, "identity": text(identity), "target": target,
              "answers": answers, "form_version": text(form_version),
              "policy_version": text(policy_version), "assets": assets}
    return strict_json(canonical(bundle)), digest(bundle)


def release_bundle(connector_id, account_id, revision, caption, assets, rights):
    if type(assets) is not list or not assets or type(rights) is not dict:
        raise ContractError("explicit assets and rights required")
    for asset in assets:
        record = rights.get(asset.get("sha256"), {})
        if record.get("status") != "cleared":
            raise ContractError("rights not cleared")
        for key in ("evidence_ref", "reviewer_ref", "inspection_ref"):
            text(record.get(key), key)
    bundle = {"schema_version": 1, "connector_id": text(connector_id),
              "provider_account_id": text(account_id), "config_revision": text(revision),
              "caption": text(caption), "assets": assets, "rights": rights}
    return strict_json(canonical(bundle)), digest(bundle)
