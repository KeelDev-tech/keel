"""Local receipt observations with an explicit, injected provider trust boundary.

This module never fetches mail or sends applications. Imported mail and manual
attestations are observations, even if their payload says `verified`. Provider
acceptance requires an application-owned validator on every grading call.
"""
import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys

_ENGINES = str(Path(__file__).resolve().parents[1])
if _ENGINES not in sys.path:
    sys.path.insert(0, _ENGINES)
from typing import Callable, Mapping

from safe_io import atomic_json, aware_time, digest, file_lock, read_json, utc_now

KINDS = {"imported_mail", "manual_attestation", "provider_receipt"}
OUTCOMES = {"AUTO_ACK", "REJECTION", "INTERVIEW_INVITE", "ASSESSMENT", "INFO_REQUEST", "OFFER", "OTHER"}


def _identifier(value, *, optional=False):
    if optional and value in (None, ""):
        return ""
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 512:
        raise ValueError("invalid receipt identifier")
    return value


def normalize_receipt(record, *, now=None):
    if not isinstance(record, dict):
        raise ValueError("receipt must be an object")
    now = now or utc_now()
    if now.tzinfo is None:
        raise ValueError("now requires timezone")
    kind = record.get("kind")
    if kind not in KINDS:
        raise ValueError("unknown receipt kind")
    received, recorded = aware_time(record.get("received_at")), aware_time(record.get("recorded_at"))
    if not received <= recorded <= now:
        raise ValueError("receipt timestamps are future or out of order")
    raw_digest = record.get("content_sha256")
    if not isinstance(raw_digest, str) or not re.fullmatch("[0-9a-f]{64}", raw_digest):
        raise ValueError("content_sha256 must be a SHA-256 digest")
    outcome = record.get("outcome")
    if outcome not in OUTCOMES:
        raise ValueError("invalid receipt outcome")
    # Whitelist: untrusted `verified`/authentication flags cannot enter authority.
    clean = {"kind": kind, "source": _identifier(record.get("source")),
             "receipt_id": _identifier(record.get("receipt_id")),
             "received_at": received.isoformat(), "recorded_at": recorded.isoformat(),
             "content_sha256": raw_digest, "outcome": outcome}
    for field in ("application_id", "role_id", "attempt_id"):
        clean[field] = _identifier(record.get(field), optional=True)
    for field in ("observed_identity", "resolved_identity"):
        if field in record:
            identity = record[field]
            if not isinstance(identity, dict):
                raise ValueError("receipt identity metadata must be an object")
            clean[field] = {key: _identifier(identity.get(key), optional=True)
                            for key in ("role_id", "attempt_id", "application_id")}
    if "observed_identity" in clean and any(clean[key] != clean["observed_identity"][key]
                                             for key in ("role_id", "attempt_id", "application_id")):
        raise ValueError("observed receipt identifiers disagree")
    return clean


class ReceiptStore:
    """Locked atomic local intake; conflicting replays permanently hold the key.

    The store is not tamper-proof against its OS account. No persisted field is
    sufficient for provider trust; host validation is rerun on each report.
    """
    def __init__(self, path):
        self.path = Path(path)

    def _read(self):
        data = read_json(self.path, missing={"version": 1, "receipts": {}, "conflicts": {}})
        if (not isinstance(data, dict) or data.get("version") != 1
                or not isinstance(data.get("receipts"), dict)
                or not isinstance(data.get("conflicts"), dict)):
            raise ValueError("malformed receipt store")
        for key, value in data["receipts"].items():
            clean = normalize_receipt(value)
            if clean != value or key != digest([value["source"], value["receipt_id"]]):
                raise ValueError("receipt store integrity mismatch")
        if any(key not in data["receipts"] or not isinstance(value, list)
               or any(not isinstance(item, str) or not re.fullmatch("[0-9a-f]{64}", item) for item in value)
               for key, value in data["conflicts"].items()):
            raise ValueError("malformed receipt conflict records")
        pending = data.setdefault("events", {})
        if not isinstance(pending, dict):
            raise ValueError("malformed receipt outbox")
        for key, event in pending.items():
            if (key not in data["receipts"] or not isinstance(event, dict)
                    or event.get("event_id") != "inbox:" + key
                    or event.get("event_type") != "employer_response"
                    or event.get("role_id") != (data["receipts"][key].get("resolved_identity") or data["receipts"][key])["role_id"]
                    or not isinstance(event.get("details"), dict)
                    or event["details"].get("receipt_key") != key):
                raise ValueError("receipt outbox integrity mismatch")
        return data

    def put(self, record, *, now=None, dry_run=False):
        clean = normalize_receipt(record, now=now)
        key = digest([clean["source"], clean["receipt_id"]])
        with file_lock(str(self.path) + ".lock"):
            data = self._read()
            previous = data["receipts"].get(key)
            # Re-reading identical receipt content at a later time is a replay.
            semantic = lambda item: {k: v for k, v in item.items() if k != "recorded_at"}
            if previous and semantic(previous) != semantic(clean):
                data["conflicts"][key] = sorted(set(data["conflicts"].get(key, []) + [digest(clean)]))
                if not dry_run:
                    atomic_json(self.path, data)
                return {"status": "held_conflict", "key": key}
            if key in data["conflicts"]:
                return {"status": "held_conflict", "key": key}
            if previous:
                return {"status": "duplicate", "key": key}
            data["receipts"][key] = clean
            if not dry_run:
                atomic_json(self.path, data)
            return {"status": "would_record" if dry_run else "recorded", "key": key}

    def freeze_event(self, key, event):
        """Persist the first event payload before append; retries reuse it exactly.

        Mutable ledger scores, titles or company labels must never change an
        already prepared event under its stable identifier.
        """
        with file_lock(str(self.path) + ".lock"):
            data = self._read()
            if key not in data["receipts"] or key in data["conflicts"]:
                raise ValueError("receipt missing or held for review")
            if key not in data["events"]:
                if (event.get("event_id") != "inbox:" + key
                        or event.get("event_type") != "employer_response"
                        or event.get("role_id") != (data["receipts"][key].get("resolved_identity") or data["receipts"][key])["role_id"]
                        or event.get("details", {}).get("receipt_key") != key):
                    raise ValueError("event must bind the receipt")
                data["events"][key] = event
                atomic_json(self.path, data)
            return data["events"][key]

    def snapshot(self):
        with file_lock(str(self.path) + ".lock"):
            data = self._read()
        return [(key, value, key in data["conflicts"]) for key, value in data["receipts"].items()]


@dataclass(frozen=True)
class ProviderValidation:
    """Result of a trusted host check, bound to both complete input records.

    The host must authenticate the provider evidence and account/recipient and
    establish acceptance of this exact application and attempt. Constructing this
    object without those checks does not implement a trustworthy adapter.
    """
    receipt_digest: str
    claim_digest: str
    provider: str
    checked_at: str
    accepted: bool


ProviderValidator = Callable[[dict, dict], ProviderValidation]


def grade_claim(claim, snapshot, *, now=None,
                validators: Mapping[str, ProviderValidator] | None = None):
    """Return observed/manual/provider counts for an exactly bound claim.

    Validators are supplied as code by the trusted host, never loaded from JSON,
    message headers, import paths, user flags or environment variables.
    """
    now, validators = now or utc_now(), validators or {}
    result = {"observed": False, "manual_attested": False, "provider_verified": False,
              "held": False, "warnings": []}
    claim_time = aware_time(claim.get("date_submitted") or claim.get("ts"))
    if claim_time > now:
        return result
    for _, receipt, conflict in snapshot:
        # A role identity is mandatory; every provided identity must agree, and
        # every claim application/attempt identity must be echoed by the receipt.
        if not claim.get("role_id") or receipt.get("role_id") != claim["role_id"]:
            continue
        if any((claim.get(field) or receipt.get(field)) and claim.get(field) != receipt.get(field)
               for field in ("attempt_id", "application_id")):
            continue
        if not claim_time <= aware_time(receipt["received_at"]) <= now:
            continue
        if conflict:
            result["held"] = True
            result["warnings"].append("Conflicting receipt identity requires review")
            continue
        result["observed"] = True
        result["manual_attested"] |= receipt["kind"] == "manual_attestation"
        validator = validators.get(receipt["source"])
        if receipt["kind"] != "provider_receipt" or not callable(validator):
            continue
        # Exact attempt binding is required for independent acceptance. Legacy
        # role-only claims can be observed but must be reconciled before trust.
        if not claim.get("attempt_id") or receipt["outcome"] != "AUTO_ACK":
            continue
        try:
            decision = validator(dict(receipt), dict(claim))
            valid = (isinstance(decision, ProviderValidation) and decision.accepted is True
                     and decision.receipt_digest == digest(receipt)
                     and decision.claim_digest == digest(claim)
                     and bool(_identifier(decision.provider))
                     and aware_time(receipt["received_at"]) <= aware_time(decision.checked_at) <= now)
            result["provider_verified"] |= valid
            if not valid:
                result["warnings"].append("Provider validator did not establish acceptance")
        except Exception as exc:
            result["warnings"].append("Provider validation unavailable: " + type(exc).__name__)
    if result["held"]:
        result["provider_verified"] = False
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--store", required=True)
    parser.add_argument("--input", required=True, help="JSON receipt observation (no trust flags)")
    args = parser.parse_args(argv)
    print(json.dumps(ReceiptStore(args.store).put(read_json(args.input)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
