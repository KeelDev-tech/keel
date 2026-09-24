#!/usr/bin/env python3
"""Durable pre-submit intent + UNKNOWN attempt state, shared by the API and
browser lanes (audit finding F22, P0 — CODE_CONFIRMED 2026-09-17).

Core integrity invariant: the pipeline must NEVER risk a duplicate
application. A submission attempt whose outcome is ambiguous — an
exception or timeout after the POST may already have been accepted by the
provider — is NEVER reported as an ordinary failure and NEVER retried on
another transport. The attempt is persisted as UNKNOWN under a stable
attempt_id; NEITHER lane may start a new attempt for the lead until
reconciliation resolves the SAME attempt_id to SUBMITTED or FAILED on
provider-correlated evidence.

State machine (per attempt_id):
    INTENT -> UNKNOWN -> SUBMITTED | FAILED
    INTENT -> SUBMITTED | FAILED        (clean round trip, unambiguous)
    UNKNOWN -> UNKNOWN                  (probe inconclusive; stays barred)

Terminal states (SUBMITTED, FAILED) are immutable. INTENT and UNKNOWN are
"open": while open for a role_id, the API lane refuses new live attempts
and the browser lane refuses to launch (claim_packet) — reconciliation
only.

- INTENT is recorded BEFORE the POST. A crash between the write and the
  POST leaves an INTENT with no verdict; recovery treats it as UNKNOWN
  (fail closed: the POST may have happened).
- Reconciliation (reconcile_unknown) takes an injected probe callable and
  NEVER issues a POST and NEVER launches a browser. The probe returns a
  verdict with provider-correlated evidence; terminal transitions require
  evidence (SUBMITTED additionally requires confirmation text).

Store: ~/workspace/job-pipeline/hidden_files/submit-intents.json
(dict keyed by attempt_id, atomic tmp+os.replace writes, fcntl lock file
around read-modify-write so the apply_loop worker pool and concurrent
lane processes cannot interleave).

Relation to F17's submission-outcome contract: if/when that contract
lands, it should read these intent records as the attempt-identity source
(attempt_id is the join key). This module defines no outcome vocabulary
beyond the attempt lifecycle, so there is no namespace conflict.

CLI:
    python3 submit_intent.py --resume
        list open (INTENT/UNKNOWN) intents for crash recovery
    python3 submit_intent.py --reconcile <attempt_id>
        run the default (non-POST, non-browser) probe and resolve
    python3 submit_intent.py --show <role_id>
        show the open intent for a role, if any

RETIREMENT (Workstream A, 2026-09-18 — Keel Blocker Resolution Directive
§1): the API lane is retired. record_intent() refuses transport
"api"/"api-direct" via engines/api_direct_policy.py (ApiDirectRetired)
BEFORE minting any attempt identity — no new API-lane attempt can start,
and an ambiguous attempt is NEVER retried on another transport. The
ambiguous-attempt state machine below (INTENT -> UNKNOWN -> SUBMITTED |
FAILED, reconciliation without POST/browser) remains the authoritative
path for EXISTING records: UNKNOWN stays UNKNOWN until reconciled on
provider-correlated evidence; history is never reinterpreted.
"""

import fcntl
import hashlib
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from keel_paths import HOME  # noqa: E402 — repo path convention
import api_direct_policy  # noqa: E402 — Workstream A retirement gate (import-free module)
STORE_PATH = os.path.join(HOME, "hidden_files", "submit-intents.json")
LOCK_PATH = STORE_PATH + ".lock"

_store_dir_override = None


def set_store_dir(path):
    """Test hook: redirect the intent store + lock file. Production code
    never calls this (mirrors the submission_outcome module's hook)."""
    global _store_dir_override, STORE_PATH, LOCK_PATH
    _store_dir_override = path
    os.makedirs(path, exist_ok=True)
    STORE_PATH = os.path.join(path, "submit-intents.json")
    LOCK_PATH = STORE_PATH + ".lock"


def reset_store_dir():
    """Restore the default store paths after a test."""
    global _store_dir_override, STORE_PATH, LOCK_PATH
    _store_dir_override = None
    STORE_PATH = os.path.join(HOME, "hidden_files", "submit-intents.json")
    LOCK_PATH = STORE_PATH + ".lock"

# An INTENT older than this with no verdict is certainly a crashed attempt.
# It is still treated as OPEN (fail closed) — resume/reconcile is the exit.
INTENT_STALE_S = 15 * 60

STATES = ("INTENT", "UNKNOWN", "SUBMITTED", "FAILED")
TERMINAL = ("SUBMITTED", "FAILED")
OPEN = ("INTENT", "UNKNOWN")

_TRANSITIONS = {
    "INTENT": ("UNKNOWN", "SUBMITTED", "FAILED"),
    "UNKNOWN": ("UNKNOWN", "SUBMITTED", "FAILED"),
    "SUBMITTED": (),
    "FAILED": (),
}


def _utcnow():
    return datetime.now(timezone.utc)


def _ts():
    return _utcnow().isoformat()


def _attempt_id(role_id, content_digest="", launch_ts_iso=None):
    """Content-bound attempt identity (Keel 0.4 P1 §3.1).

    attempt_id = "si-" + sha256(role_id + "|" + packet_digest + "|"
                 + launch_ts_iso).hexdigest()[:16] + "-" + UTC-stamp

    The 16-hex prefix is deterministic for (role, content, launch instant):
    two attempts on the same role+packet in the same second collide into
    the same prefix — which is exactly what makes a replay visible instead
    of silently unique. The trailing UTC stamp keeps human sortability and
    the "si-" prefix keeps dashboard/blackboard tolerance (the old shape
    was si-<role>-<stamp>-<rand>; parsers must tolerate the new shape).
    """
    ts = _utcnow()
    stamp = ts.strftime("%Y%m%dT%H%M%S")
    launch_iso = launch_ts_iso or ts.replace(microsecond=0).isoformat()
    # Second precision: two attempts on the same role+packet in the same
    # second collide into the same prefix (spec §4) instead of differing
    # on microseconds. Unparseable strings pass through unchanged (still
    # deterministic for identical input).
    try:
        launch_iso = datetime.fromisoformat(
            launch_iso.replace("Z", "+00:00")).replace(
            microsecond=0).isoformat()
    except (ValueError, TypeError):
        pass
    material = f"{role_id or ''}|{content_digest or ''}|{launch_iso}"
    prefix = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"si-{prefix}-{stamp}"


def _content_digest(packet):
    """sha256 hex of the canonical packet bytes for attempt-id binding.

    Accepts a dict (canonical JSON, sorted keys, compact), raw bytes, or
    an already-computed hex digest string (passed through). The top-level
    "attempt_id" key is excluded: the digest is computed from the
    PRE-STAMP packet bytes (the id cannot be an input to itself).
    """
    if packet is None:
        return ""
    if isinstance(packet, bytes):
        return hashlib.sha256(packet).hexdigest()
    if isinstance(packet, dict):
        pre = {k: v for k, v in packet.items() if k != "attempt_id"}
        canon = json.dumps(pre, sort_keys=True, separators=(",", ":"),
                           default=str)
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()
    s = str(packet)
    if re.fullmatch(r"[0-9a-fA-F]{16,}", s):
        return s.lower()
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@contextmanager
def _locked_store():
    """Read-modify-write context manager. The caller mutates the yielded
    store dict inside the `with` block; on clean exit it is persisted
    atomically (tmp + os.replace) under an exclusive fcntl lock, so the
    apply_loop worker pool and concurrent lane processes cannot
    interleave. Never silently loses intents: a missing store starts
    empty, but a corrupt store raises instead of being overwritten —
    losing an UNKNOWN intent is the one failure this module must not
    silently allow.

    Locking scope (adversarial note): the fcntl lock serializes writers
    on this host; durability for readers comes from the atomic
    os.replace — an unlocked reader (open_unknown_for / resume_open)
    always sees either the complete old file or the complete new file,
    never a torn write, because the replace is a single atomic rename
    on POSIX. Deployment assumption: single-node, local filesystem
    (fcntl is not reliable on NFS-style network mounts; this pipeline
    runs on one VM, so the assumption holds — do not move this store
    to network-attached storage without replacing the lock).
    """
    os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
    with open(LOCK_PATH, "a+") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            try:
                with open(STORE_PATH) as f:
                    store = json.load(f)
            except FileNotFoundError:
                store = {}
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"submit-intents store is corrupt ({e}); refusing to "
                    f"overwrite — inspect {STORE_PATH} manually")
            if not isinstance(store, dict):
                raise RuntimeError(
                    f"submit-intents store is not a dict — refusing to "
                    f"overwrite {STORE_PATH}")
            yield store
            tmp = STORE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(store, f, indent=1, sort_keys=True)
            os.replace(tmp, STORE_PATH)
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)


class _StoreUnreadable(Exception):
    """Raised when the intent store exists but cannot be read or parsed.

    Fail-closed signal: call sites that bar lanes on open intents must
    treat this as 'an open intent may exist' — never as 'no intents'.
    """


def _read_store_unlocked():
    """Read the store for pure queries.

    Missing file -> {} (no intents recorded yet). A corrupt or
    unreadable store -> raises _StoreUnreadable so call sites fail
    CLOSED: an unreadable store must bar both lanes, never read as
    'no open intents'.
    """
    try:
        with open(STORE_PATH) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise _StoreUnreadable("store root is not a JSON object")
        return d
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        raise _StoreUnreadable("intent store unreadable: %s: %s"
                               % (type(e).__name__, e))


def _append_history(rec, frm, to, reason):
    rec.setdefault("history", []).append({
        "ts": _ts(), "from": frm, "to": to,
        "reason": (reason or "")[:500],
    })
    rec["state"] = to
    rec["updated_ts"] = _ts()


class OpenIntentExists(RuntimeError):
    """Raised by record_intent when an OPEN (INTENT/UNKNOWN) intent already
    exists for the role_id. Two attempts on the same role fail closed: the
    second never starts while the first is open, on any path."""


class AttemptIdCollision(Exception):
    """Raised by record_intent when the computed attempt_id already exists
    in the store.

    The attempt_id is second-granular by design: a second record_intent
    for the same role+content in the same second would otherwise blindly
    overwrite the stored record (reproduced: INTENT -> mark_submitted ->
    a same-second record_intent resurrected INTENT and destroyed history,
    violating "terminal states are immutable"). The collision is refused,
    never resolved by replacement — the caller must reconcile the
    existing attempt or retry under a new attempt identity."""


def record_intent(role_id, company, transport, bundle_digest, page_ref=None,
                  packet=None):
    """Persist a pre-submit intent BEFORE the POST. Returns attempt_id.

    bundle_digest binds the intent to the exact payload fingerprint that
    was human-approved; page_ref carries non-sensitive ATS routing refs
    (submit_path, confirmation_path, board, job_id) for reconciliation
    probes. Never stores answers, tokens, or credentials.

    packet (Keel 0.4 P1 §3.1): the launch packet dict (or raw bytes) at
    claim time. The content-bound attempt_id is derived from the PRE-STAMP
    packet bytes (top-level "attempt_id" excluded); when packet is None
    the bundle_digest is used as the content binding instead.

    RETIREMENT (Workstream A, 2026-09-18): the API lane is retired.
    transport "api"/"api-direct" raises api_direct_policy.ApiDirectRetired
    BEFORE any intent is minted — no new API-lane attempt can start.

    Fail-closed: when an OPEN intent already exists for role_id — or the
    store is unreadable/corrupt — raises OpenIntentExists instead of
    minting a second attempt identity (an unreadable store must bar the
    lead, never read as "no open intents"). The open check runs INSIDE
    the locked store transaction: two concurrent callers cannot both
    pass the check and mint two attempts. A second-granular attempt_id
    collision raises AttemptIdCollision — an existing record is never
    replaced (terminal states are immutable).
    """
    if (transport or "").strip().lower() in ("api", "api-direct"):
        # Workstream A retirement gate: consult BEFORE minting. Raises
        # api_direct_policy.ApiDirectRetired (a RuntimeError subclass) —
        # the retired lane cannot start a new attempt on any role.
        api_direct_policy.check("submission")
    content = _content_digest(packet) if packet is not None \
        else (bundle_digest or "")
    attempt_id = _attempt_id(role_id, content)
    rec = {
        "attempt_id": attempt_id,
        "role_id": role_id or "",
        "company": company or "",
        "transport": transport or "api",
        "bundle_digest": bundle_digest or "",
        "page_ref": page_ref or {},
        "state": "INTENT",
        "created_ts": _ts(),
        "updated_ts": _ts(),
        "history": [{"ts": _ts(), "from": None, "to": "INTENT",
                     "reason": "pre-submit intent recorded before POST"}],
    }
    try:
        with _locked_store() as store:
            # Atomic duplicate-open check: the store is exclusively locked
            # for this whole read-check-write, so a second concurrent
            # caller cannot slip a second open intent past us.
            if role_id:
                cands = [r for r in store.values()
                         if r.get("role_id") == role_id
                         and r.get("state") in OPEN]
                if cands:
                    cands.sort(key=lambda r: r.get("updated_ts") or "",
                               reverse=True)
                    open_rec = cands[0]
                    raise OpenIntentExists(
                        f"open intent {open_rec.get('attempt_id')} "
                        f"({open_rec.get('state')}) already exists for role "
                        f"{role_id!r} — refusing a second attempt; reconcile "
                        f"the open intent to a terminal state first")
            if attempt_id in store:
                # Second-granular attempt_id collision: never replace an
                # existing record — a blind overwrite resurrects terminal
                # states and destroys history (silent-defect sweep
                # 2026-09-19). Fail loud instead.
                raise AttemptIdCollision(
                    f"attempt_id {attempt_id} already exists for role "
                    f"{store[attempt_id].get('role_id')!r} (state "
                    f"{store[attempt_id].get('state')}) — refusing to "
                    "replace a stored record; reconcile the existing "
                    "attempt or retry under a new identity")
            store[attempt_id] = rec
    except AttemptIdCollision:
        raise
    except OpenIntentExists:
        raise
    except RuntimeError as e:
        # Corrupt/unreadable store: fail closed — refuse to mint an
        # attempt rather than read the damage as "no open intents".
        raise OpenIntentExists(
            f"submit-intents store unreadable ({e}); refusing to mint a "
            f"second attempt for role {role_id!r} — reconcile the store "
            f"manually") from e
    return attempt_id


def all_records():
    """Every intent record (any state), oldest first. Read-only copies.

    Used by the startup reconciliation's ledger cross-check. Fail-closed:
    an unreadable store raises _StoreUnreadable — never an empty list.
    """
    store = _read_store_unlocked()
    recs = list(store.values())
    recs.sort(key=lambda r: r.get("created_ts") or "")
    return json.loads(json.dumps(recs))


def flag_inconsistent(attempt_id, name, detail):
    """Record a NAMED inconsistent state on an intent WITHOUT changing its
    lifecycle state (Keel 0.4 P1 §3.2 step 6).

    E.g. "SUBMITTED-without-ledger": the provider confirmed the submit and
    the intent is terminal-SUBMITTED, but the ledger row write failed and
    the lease was deliberately NOT released. The startup reconciliation
    repairs these explicitly (ledger cross-check); the name is the repair
    key, never guessed. Raises KeyError for an unknown attempt_id.
    """
    with _locked_store() as store:
        rec = store.get(attempt_id)
        if not rec:
            raise KeyError(f"no such submit intent: {attempt_id}")
        entry = {"ts": _ts(), "name": name,
                 "detail": (detail or "")[:500]}
        rec.setdefault("inconsistent_states", []).append(entry)
        rec.setdefault("history", []).append(
            {"ts": _ts(), "from": rec.get("state"), "to": rec.get("state"),
             "reason": f"inconsistent state recorded: {name}"[:500]})
        rec["updated_ts"] = _ts()
        return json.loads(json.dumps(rec))


def get(attempt_id):
    """Fetch one intent record (read-only copy). None if absent."""
    rec = _read_store_unlocked().get(attempt_id)
    return json.loads(json.dumps(rec)) if rec else None


def _transition(attempt_id, to_state, reason, require=None):
    if to_state not in STATES:
        raise ValueError(f"unknown state {to_state!r}")
    result = {}
    with _locked_store() as store:
        rec = store.get(attempt_id)
        if not rec:
            raise KeyError(f"no such submit intent: {attempt_id}")
        frm = rec.get("state")
        if frm in TERMINAL:
            # Terminal states are immutable: a late verdict on an already-
            # resolved attempt is a no-op, never a rewrite (rewriting
            # SUBMITTED could un-bar a duplicate). The ignored verdict is
            # noted in history so the audit trail shows it arrived.
            rec.setdefault("history", []).append({
                "ts": _ts(), "from": frm, "to": frm,
                "reason": (f"late {to_state} verdict ignored: already "
                           f"{frm}")[:500]})
            rec["updated_ts"] = _ts()
            return dict(rec)
        if to_state not in _TRANSITIONS.get(frm, ()):
            raise ValueError(
                f"illegal submit-intent transition {frm} -> {to_state} "
                f"(attempt {attempt_id})")
        if require == "confirmation" and not (reason or "").strip():
            raise ValueError("SUBMITTED requires confirmation text")
        if require == "reason" and not (reason or "").strip():
            raise ValueError("FAILED requires a reason")
        _append_history(rec, frm, to_state, reason)
        result = dict(rec)
    return result


def mark_unknown(attempt_id, reason, evidence=None):
    """Ambiguous outcome (exception/timeout after the POST may have been
    accepted). The attempt is UNKNOWN — never FAILED — so no lane retries
    or switches transport until reconciliation resolves it."""
    rec = _transition(attempt_id, "UNKNOWN", reason, require="reason")
    if evidence:
        with _locked_store() as store:
            store[attempt_id].setdefault("evidence", {}).update(evidence)
            store[attempt_id]["updated_ts"] = _ts()
        rec = get(attempt_id)
    return rec


def mark_submitted(attempt_id, confirmation, evidence=None):
    """Authoritative success. Requires confirmation text (fail closed)."""
    if not (confirmation or "").strip():
        raise ValueError("mark_submitted requires confirmation text")
    rec = _transition(attempt_id, "SUBMITTED", confirmation[:500],
                       require="confirmation")
    if evidence:
        with _locked_store() as store:
            store[attempt_id].setdefault("evidence", {}).update(evidence)
            store[attempt_id]["updated_ts"] = _ts()
        rec = get(attempt_id)
    return rec


def mark_failed(attempt_id, reason, evidence=None):
    """Authoritative failure (provider verdict that nothing was accepted,
    or a reconciled probe verdict). Requires a reason. Only an
    authoritative FAILED re-opens the browser fallback."""
    rec = _transition(attempt_id, "FAILED", reason, require="reason")
    if evidence:
        with _locked_store() as store:
            store[attempt_id].setdefault("evidence", {}).update(evidence)
            store[attempt_id]["updated_ts"] = _ts()
        rec = get(attempt_id)
    return rec


def open_unknown_for(role_id):
    """Newest OPEN (INTENT or UNKNOWN) intent for a role_id, or None.

    Both lanes call this before starting anything: an open intent bars a
    new API attempt AND a browser launch. Fail-closed: if the store cannot
    be read, returns a synthetic UNKNOWN sentinel so callers refuse rather
    than guess (a missing store read must never un-bar a maybe-submitted
    attempt).
    """
    if not role_id:
        return None
    try:
        store = _read_store_unlocked()
        cands = [r for r in store.values()
                 if r.get("role_id") == role_id and r.get("state") in OPEN]
        if not cands:
            return None
        cands.sort(key=lambda r: r.get("updated_ts") or "", reverse=True)
        return cands[0]
    except Exception:
        # Fail closed: an unreadable store must not un-bar the lead.
        return {"attempt_id": "si-UNREADABLE-STORE",
                "role_id": role_id,
                "state": "UNKNOWN",
                "synthetic": True,
                "reason": "submit-intents store unreadable; refusing"}


def resume_open():
    """All OPEN intents, oldest first — the crash-recovery driver.

    Recovery resumes RECONCILIATION under the same attempt_id; it never
    re-POSTs and never launches a browser. INTENT records (crashed before
    any verdict) are returned with a flag so the driver reconciles them
    as UNKNOWN-equivalent.

    Fail-closed: a corrupt/unreadable store raises _StoreUnreadable —
    the driver must surface it and refuse to proceed. Returning an empty
    list here would silently un-bar every lead, which is the failure
    this module exists to prevent."""
    store = _read_store_unlocked()
    recs = [r for r in store.values() if r.get("state") in OPEN]
    recs.sort(key=lambda r: r.get("created_ts") or "")
    return recs


def is_stale_intent(rec):
    """True when an INTENT is old enough that no verdict is coming from
    the process that wrote it (crashed mid-attempt). Still OPEN — the
    exit is reconcile, never silent retry."""
    if not rec or rec.get("state") != "INTENT":
        return False
    try:
        created = datetime.fromisoformat(rec.get("created_ts") or "")
    except ValueError:
        return True
    return _utcnow() - created > timedelta(seconds=INTENT_STALE_S)


# ---------------------------------------------------------------- reconcile

def reconcile_unknown(attempt_id, probe):
    """Resolve an UNKNOWN (or stale INTENT) attempt under the SAME
    attempt_id. Never issues a POST; never launches a browser.

    probe: zero-arg callable returning
        {"verdict": "submitted" | "failed" | "still_unknown",
         "evidence": {...}, "confirmation": "<text>"}  # confirmation only
                                                       # when verdict=submitted
    - "submitted" requires provider-correlated evidence AND confirmation
      text, else the probe's claim is rejected and the attempt stays
      UNKNOWN (fail closed: an unverifiable 'it worked' never un-bars).
    - "failed" requires evidence of a provider verdict (or probe notes);
      only this re-opens the browser fallback.
    - "still_unknown" (or any probe exception / malformed verdict) keeps
      UNKNOWN and appends the probe note to history — the lead stays
      barred from both lanes.

    Returns (new_state, record). Idempotent on terminal attempts.
    """
    if not callable(probe):
        raise ValueError("reconcile_unknown requires a probe callable")
    rec = get(attempt_id)
    if not rec:
        raise KeyError(f"no such submit intent: {attempt_id}")
    if rec.get("state") in TERMINAL:
        _note(attempt_id,
              f"late reconcile verdict ignored: already {rec.get('state')}")
        return rec["state"], get(attempt_id)  # idempotent: already resolved
    if rec.get("state") not in OPEN:
        raise ValueError(f"cannot reconcile attempt in state {rec.get('state')}")
    try:
        out = probe() or {}
    except Exception as e:
        out = {"verdict": "still_unknown",
               "evidence": {"probe_error": f"{type(e).__name__}: {e}"[:300]}}
    verdict = (out.get("verdict") or "").strip().lower()
    evidence = out.get("evidence") if isinstance(out.get("evidence"), dict) else {}
    if verdict == "submitted":
        confirmation = (out.get("confirmation") or "").strip()
        if not confirmation or not evidence:
            _note(attempt_id, "probe claimed submitted without "
                  "confirmation+evidence; staying UNKNOWN (fail closed)",
                  evidence)
            return "UNKNOWN", get(attempt_id)
        return "SUBMITTED", mark_submitted(attempt_id, confirmation,
                                           evidence=evidence)
    if verdict == "failed":
        if not evidence:
            _note(attempt_id, "probe claimed failed without evidence; "
                  "staying UNKNOWN (fail closed)", evidence)
            return "UNKNOWN", get(attempt_id)
        return "FAILED", mark_failed(
            attempt_id,
            (out.get("reason") or evidence.get("note")
             or "reconciled: provider evidence shows no submission accepted")[:500],
            evidence=evidence)
    # still_unknown or anything unrecognized: stay barred, note the probe.
    _note(attempt_id,
          f"reconcile probe verdict={verdict or 'missing'}: still UNKNOWN",
          evidence)
    return "UNKNOWN", get(attempt_id)


def _note(attempt_id, reason, evidence=None):
    with _locked_store() as store:
        rec = store.get(attempt_id)
        if not rec:
            return
        rec.setdefault("history", []).append(
            {"ts": _ts(), "from": rec.get("state"), "to": rec.get("state"),
             "reason": (reason or "")[:500]})
        if evidence:
            rec.setdefault("evidence", {}).update(evidence)
        rec["updated_ts"] = _ts()


# ------------------------------------------------- default (non-POST) probe

def greenhouse_confirmation_probe(page_ref):
    """Default production probe for Greenhouse HTTP attempts.

    NON-DESTRUCTIVE ONLY: fetches the confirmation page over GET and looks
    for explicit applicant-facing confirmation markers. It NEVER POSTs and
    NEVER launches a browser. Fail-closed by construction: unless explicit
    confirmation evidence is found it returns still_unknown — an
    unverifiable attempt stays barred rather than risking a duplicate.

    page_ref: the intent's stored page_ref (confirmation_path, board,
    job_id). Stronger provider adapters (employer 'already applied'
    signals, ATS API job records) can be added as probe callables without
    touching the state machine.
    """
    def _probe():
        evidence = {"probe": "greenhouse_confirmation_probe",
                    "non_post": True}
        try:
            cpath = (page_ref or {}).get("confirmation_path")
            if not cpath:
                evidence["note"] = "no confirmation_path in page_ref"
                return {"verdict": "still_unknown", "evidence": evidence}
            text = None
            for host in ("https://job-boards.greenhouse.io",
                         "https://boards.greenhouse.io"):
                try:
                    import urllib.request
                    req = urllib.request.Request(
                        host + cpath,
                        headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=30) as r:
                        html = r.read().decode("utf-8", "replace")
                    import re as _re
                    clean = _re.sub(r"<[^>]+>", " ", html)
                    clean = _re.sub(r"\s+", " ", clean).strip()
                    m = _re.search(
                        r".{0,80}(thank you|application.{0,20}received|"
                        r"successfully submitted).{0,160}", clean, _re.I)
                    if m:
                        text = m.group(0).strip()
                        evidence.update(
                            {"host": host, "confirmation_marker": text[:200]})
                        break
                    evidence["note"] = ("confirmation page fetched but no "
                                        "applicant confirmation marker")
                except Exception as e:
                    evidence.setdefault("fetch_errors", []).append(
                        f"{host}: {type(e).__name__}")
                    continue
            if text:
                # Correlated evidence: the provider's own confirmation page
                # carries an explicit submission marker for this posting.
                return {"verdict": "submitted", "evidence": evidence,
                        "confirmation": text[:300]}
            return {"verdict": "still_unknown", "evidence": evidence}
        except Exception as e:
            evidence["probe_error"] = f"{type(e).__name__}: {e}"[:200]
            return {"verdict": "still_unknown", "evidence": evidence}
    return _probe


# ---------------------------------------------------------------- CLI

def main(argv):
    import argparse
    ap = argparse.ArgumentParser(
        description="Submit-intent store: inspect and reconcile UNKNOWN "
                    "attempts (F22). Never POSTs, never launches a browser.")
    ap.add_argument("--resume", action="store_true",
                    help="list open (INTENT/UNKNOWN) intents, oldest first")
    ap.add_argument("--reconcile", metavar="ATTEMPT_ID",
                    help="reconcile one UNKNOWN attempt with the default "
                         "non-POST probe")
    ap.add_argument("--show", metavar="ROLE_ID",
                    help="show the open intent for a role, if any")
    args = ap.parse_args(argv)
    if args.resume:
        recs = resume_open()
        print(json.dumps(recs, indent=1))
        return 0
    if args.show:
        print(json.dumps(open_unknown_for(args.show), indent=1))
        return 0
    if args.reconcile:
        rec = get(args.reconcile)
        if not rec:
            print(json.dumps({"error": "no such intent",
                              "attempt_id": args.reconcile}))
            return 1
        probe = greenhouse_confirmation_probe(rec.get("page_ref") or {})
        state, rec2 = reconcile_unknown(args.reconcile, probe)
        # Driver contract: reconciliation resolves the ATTEMPT, not the
        # lead. The driver that invoked --reconcile MUST honor next_action:
        # SUBMITTED means the application landed — mark the lead submitted
        # through the canonical record_outcome path with the confirmation
        # evidence; NEVER re-POST or relaunch. FAILED re-opens the lead
        # for selection (browser fallback is legal again). UNKNOWN keeps
        # both lanes barred; run --reconcile again later.
        if state == "SUBMITTED":
            next_action = ("mark the lead SUBMITTED via record_outcome.py "
                           "with the confirmation evidence from this "
                           "record; do NOT re-POST, do NOT relaunch, do NOT "
                           "emit a duplicate submission")
        elif state == "FAILED":
            next_action = ("attempt authoritatively failed; the lead may "
                           "re-enter selection and the browser fallback is "
                           "legal again")
        else:
            next_action = ("attempt still UNKNOWN; both lanes stay barred; "
                           "re-run --reconcile later")
        print(json.dumps({"attempt_id": args.reconcile, "state": state,
                          "next_action": next_action,
                          "record": rec2}, indent=1))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
