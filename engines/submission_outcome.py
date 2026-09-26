#!/usr/bin/env python3
"""Submission outcome contract — F17 fix (2026-09-17).

ONE decision point for "did this direct-submit HTTP attempt prove an
application was submitted?". Both api_submit.py and greenhouse_direct.py
route their POST verdicts through classify_attempt().

Outcomes:
  SUBMITTED — ONLY with provider/attempt-correlated receipt evidence: a
      confirmation ID, application URL, or receipt token that matches the
      attempt (job/board/fingerprint context). Bare HTTP 2xx is NEVER
      enough — a 2xx with no confirmation, an HTML error page, or a
      malformed body is not a submission.
  UNKNOWN   — ambiguous 2xx (missing confirmation, HTML error page,
      malformed JSON), HTTP 5xx, or a transport failure that may have
      reached the server (timeout, connection reset). Opens a durable
      reconciliation HOLD (append-only JSONL, attempt_id-keyed) so a later
      pass can resolve it. UNKNOWN never becomes SUBMITTED silently and
      must never trigger a fallback submit — the POST may already have
      been processed server-side, and a blind second submit risks a
      duplicate application.
  FAILED    — the server refused (verdict code, non-2xx non-5xx), or the
      transport failed BEFORE any bytes were sent (DNS failure, connection
      refused). Safe for a fallback lane to handle per its own rules.

Idempotency ("a correlated positive receipt counts once"): every
SUBMITTED classification is recorded in an append-only outcomes log.
A receipt value already recorded as SUBMITTED returns duplicate=True —
callers must not re-announce it, re-ledger it, or call record_outcome
for it again.

Correlation model and known limits (red-team 2026-09-17):
  A confirmation-page receipt proves three things: (1) THIS attempt's
  POST was answered 2xx by the provider's submit endpoint; (2) the
  provider's confirmation page FOR THIS JOB POSTING (confirmation_path
  comes from this attempt's own page data) answers 200; (3) that page
  carries a positive confirmation marker and no error marker, at fetch
  time inside this attempt's verification window.
  What it does NOT prove: the marker is structural, not
  cryptographically bound to the POST bytes. A provider that returns
  2xx without recording the application AND serves a statically
  rendered thank-you page would produce a false SUBMITTED. That chain
  is speculative (no such provider behavior has been observed; the
  embedded confirmationPath is a JS-app route, and a stateless GET
  typically yields the app shell, not a rendered thank-you — i.e. the
  realistic failure direction is a false UNKNOWN, which is safe).
  Defense in depth: the receipt excerpt and URL are stored in the
  durable outcome row, so every SUBMITTED is auditable post-facto
  against the provider. If a provider is ever observed serving static
  thank-you pages, its board goes on the confirmation-page denylist
  (not yet needed) and its attempts stay UNKNOWN.

Durability: holds and outcomes live under
~/workspace/job-pipeline/hidden_files/ as append-only JSONL. Holds carry
no payload, answers, or secrets — only attempt identity, routing
metadata, and a short receipt excerpt. Writes take an fcntl lock so
concurrent arms cannot interleave records. Growth is bounded by
archive_resolved_holds(): terminal RESOLVED rows (and the OPEN rows
they resolve) move to an append-only archive file, keeping the hot
file small for the scans list_open_holds() performs — see the
red-team note on hold-file exhaustion (2026-09-17).

Test hook: set_store_dir(path) redirects both logs (tests must never
touch production holds/outcomes).

Relation to the F22 submit_intent lane: that module owns the
cross-lane attempt state machine (INTENT -> UNKNOWN -> SUBMITTED |
FAILED) and bars new attempts while one is open. This module owns the
outcome VOCABULARY — what the HTTP evidence proves — and the
receipt-correlation rule. The two join on attempt_id: every hold and
outcome row here carries the attempt_id a submit_intent record can
reference, and hold page_refs carry what its reconciliation probe
needs. Neither module duplicates the other's store.
"""

import errno
import fcntl
import hashlib
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

from keel_paths import HOME  # noqa: E402 — repo path convention
import safe_http  # noqa: E402 — policy-checked transport
DEFAULT_STORE_DIR = os.path.join(HOME, "hidden_files")
OUTCOMES_LOG = "submission-outcomes.jsonl"
HOLDS_LOG = "submission-reconciliation-holds.jsonl"

SUBMITTED = "submitted"
UNKNOWN = "unknown"
FAILED = "failed"

_UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/126.0.0.0 Safari/537.36")}

# Positive confirmation markers: the page must actually SAY the
# application was received. (Same marker vocabulary the lane used
# before F17; the change is that a missing marker no longer defaults
# to success.)
_CONFIRM_RE = re.compile(
    r".{0,80}(thank you|application.{0,20}received|"
    r"successfully submitted).{0,160}", re.I | re.S)
# High-signal error markers: if the fetched page carries one of these it
# is NOT receipt evidence — even when a positive marker also matches.
# (An error page can carry polite "thank you" language: "Something went
# wrong, thank you for your patience" must never become a receipt.
# Red-team 2026-09-17.) Kept to multi-word phrases so a stray "error"
# in help text cannot veto a real confirmation; the fail-closed cost of
# a false veto is one UNKNOWN hold, never a false SUBMITTED.
_ERROR_MARKERS = (
    "something went wrong", "an error occurred", "invalid request",
    "security code", "captcha", "not found",
)

_store_dir_override = None


def set_store_dir(path):
    """Test hook: redirect the outcomes/holds logs. Production code never
    calls this."""
    global _store_dir_override
    _store_dir_override = path


def reset_store_dir():
    global _store_dir_override
    _store_dir_override = None


def _store_dir(explicit=None):
    if explicit:
        return explicit
    return _store_dir_override or DEFAULT_STORE_DIR


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


class _LockedAppend:
    """fcntl-guarded append to a JSONL log. Fail-closed on lock errors:
    an unwritable log raises — a hold that cannot be recorded must not
    be silently dropped.

    Lock semantics (red-team note 2026-09-17): fcntl.flock is bound to
    the open file DESCRIPTION, not to the .lock file's existence. The
    kernel releases the lock when the holder process dies — even on
    SIGKILL — so a stale .lock FILE on disk can never wedge the next
    acquirer. There is no lockfile-orphan failure mode here."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = open(self.path + ".lock", "w")
        fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX)
        self._fh = open(self.path, "a")
        return self._fh

    def __exit__(self, *exc):
        try:
            self._fh.close()
        finally:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
            self._lock.close()
        return False


def _read_log(name, store_dir):
    path = os.path.join(_store_dir(store_dir), name)
    rows = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue  # never let one bad line kill the reader
    except FileNotFoundError:
        pass
    return rows


class Receipt:
    """Correlated receipt evidence for one submit attempt.

    kind: "confirmation_page" | "server_token"
    value: the stable receipt identifier (confirmation URL or token) —
        the idempotency key: one value counts as one submission.
    excerpt: short human-readable confirmation snippet (truncated; no
        payload data).
    correlates_with_attempt: True only when the evidence structurally
        matches the attempt (built from the attempt's own
        confirmation_path / echoed attempt token). classify_attempt()
        requires this for SUBMITTED.
    """

    def __init__(self, kind, value, excerpt="", correlates_with_attempt=False,
                 attempt_id=None):
        self.kind = kind
        self.value = value
        self.excerpt = (excerpt or "")[:300]
        # Strings such as "false" and truthy containers are not evidence.
        self.correlates_with_attempt = correlates_with_attempt is True
        self.attempt_id = attempt_id

    def to_dict(self):
        return {"kind": self.kind, "value": self.value,
                "excerpt": self.excerpt,
                "correlates_with_attempt": self.correlates_with_attempt,
                "attempt_id": self.attempt_id}


def _correlated_receipt(receipt):
    """Validate the local evidence shape, without asserting authenticity."""
    return (isinstance(receipt, Receipt)
            and receipt.kind in ("confirmation_page", "server_token")
            and isinstance(receipt.value, str) and bool(receipt.value.strip())
            and receipt.correlates_with_attempt is True
            and isinstance(receipt.attempt_id, str)
            and bool(receipt.attempt_id.strip()))


def new_attempt_id(transport, role_id, fingerprint=""):
    """Stable, unique attempt identity: one per POST attempt. The
    reconciliation hold is keyed on this; the F22 submit_intent lane
    joins on it as well."""
    safe_role = re.sub(r"[^A-Za-z0-9_-]", "_", role_id or "norole")[:40]
    fp = (fingerprint or "")[:16]
    return "so-%s-%s-%s-%s" % (transport, safe_role, fp, uuid.uuid4().hex[:8])


def _strip_html(html):
    text = re.sub(r"<script.*?</script\s*>|<style.*?</style\s*>", " ", html,
                  flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_confirmation_receipt(page, attempt_id, timeout=30):
    """Fetch the attempt's confirmation page and build a Receipt, or None.

    A receipt requires ALL of:
      - the attempt's page carries a confirmation_path,
      - the confirmation URL answers HTTP 200,
      - the final URL after redirects is still on a Greenhouse board
        host (urllib follows 302s; a redirect elsewhere is not the
        provider's confirmation page),
      - the page text contains a positive confirmation marker
        (thank-you / application-received / successfully-submitted),
      - the page text contains no error marker (an error page's polite
        "thank you" never counts),
    and the URL is built from the attempt's OWN confirmation_path, which
    is what makes the receipt attempt-correlated.

    Returns None on any failure, missing marker, or error page — never a
    placeholder sentence. Callers must treat None as "no receipt".
    """
    path = (page or {}).get("confirmation_path")
    if not path:
        return None
    allowed_hosts = ("job-boards.greenhouse.io", "boards.greenhouse.io")
    for host in allowed_hosts:
        url = host + path
        try:
            req = urllib.request.Request("https://" + url, headers=_UA)
            with safe_http.urlopen(req, timeout=timeout) as r:
                if r.status != 200:
                    continue
                # Redirect guard (red-team 2026-09-17): urllib follows
                # 302s by default. Only accept the page when the final
                # URL is still on a Greenhouse board host AND its path
                # still matches the attempt's own confirmation_path — a
                # redirect to any other page (ToS, careers home, another
                # company's board) is not this attempt's confirmation
                # page, even if it happens to contain thank-you text.
                final_parts = urllib.parse.urlsplit(r.geturl())
                if final_parts.hostname not in allowed_hosts:
                    continue
                want_path = urllib.parse.urlsplit(path).path
                if final_parts.path != want_path:
                    continue
                html = r.read().decode("utf-8", "replace")
        except Exception:
            continue
        text = _strip_html(html)
        if any(mark in text.lower() for mark in _ERROR_MARKERS):
            # Error page: vetoes any positive-marker match outright.
            # Never a receipt — keep looking, then give up.
            continue
        m = _CONFIRM_RE.search(text)
        if not m:
            # 200 but no positive marker: could be an error/generic page.
            # Not receipt evidence — keep looking, then give up.
            continue
        return Receipt(kind="confirmation_page", value="https://" + url,
                       excerpt=m.group(0).strip(),
                       correlates_with_attempt=True,
                       attempt_id=attempt_id)
    return None


def _is_pre_send(exc):
    """True when the transport provably failed before any bytes reached
    the server: DNS resolution or TCP connect never completed."""
    if isinstance(exc, (socket.gaierror, ConnectionRefusedError)):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (socket.gaierror, ConnectionRefusedError)):
        return True
    if isinstance(exc, OSError) and exc.errno in (
            errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN,
            errno.ECONNREFUSED):
        return True
    return False


def _coerce_http_error(exc):
    """urllib raises HTTPError for non-2xx responses — that IS a server
    verdict, not a transport failure. Coerce it back to (status, resp)
    so the status classifier sees it. Returns None for anything else."""
    if not isinstance(exc, urllib.error.HTTPError):
        return None
    try:
        status = exc.code
    except Exception:
        return None
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:
        raw = ""
    try:
        resp = json.loads(raw) if raw else {}
    except ValueError:
        resp = {"_raw": raw[:500]} if raw else {}
    if not isinstance(resp, dict):
        resp = {"_raw": str(resp)[:500]}
    return status, resp


def _body_malformed(resp):
    """True when the POST response body is not a clean parsed JSON
    object — malformed JSON or an HTML error page, captured by the
    transports as {"_raw": ...} (see greenhouse_direct.submit and
    _coerce_http_error).

    Such a body is direct negative evidence about THIS POST: it vetoes
    a separately-fetched receipt. The acceptance contract requires
    2xx-with-HTML-error-page and 2xx-with-malformed-JSON to be UNKNOWN,
    never SUBMITTED."""
    return (not isinstance(resp, dict)) or ("_raw" in resp)


def _classify_status(status, resp, receipt):
    code = resp.get("code") if isinstance(resp, dict) else None
    if 200 <= status < 300 and not code:
        if _body_malformed(resp):
            # Fail closed: the server answered the POST with a body we
            # cannot read as a verdict — treat it as an error page.
            return (UNKNOWN,
                    "HTTP 2xx from the submit endpoint with a malformed "
                    "response body (unparseable JSON / HTML error page) — "
                    "the POST may have been processed; reconcile before "
                    "any retry")
        if _correlated_receipt(receipt):
            return (SUBMITTED,
                    "correlated receipt (%s): %s"
                    % (receipt.kind, receipt.value))
        return (UNKNOWN,
                "HTTP 2xx from the submit endpoint with no correlated "
                "receipt evidence (no confirmation page, no positive "
                "confirmation marker) — the POST may "
                "have been processed; reconcile before any retry")
    if 500 <= status < 600:
        # Fail closed: a 5xx may have landed the write server-side while
        # failing to render the response.
        return (UNKNOWN,
                "HTTP %s from the submit endpoint: server error after the "
                "send — the application may have been recorded; reconcile "
                "before any retry or fallback" % status)
    return (FAILED,
            "server verdict: code=%s http=%s" % (code, status))


def classify_attempt(status=None, resp=None, receipt=None, exc=None):
    """Single decision function for one submit attempt.

    Returns (outcome, reason) with outcome in {submitted, unknown,
    failed}. Exactly one of (status, resp) or exc carries the signal;
    exc takes precedence and HTTPError is coerced to a server verdict.
    """
    if exc is not None:
        coerced = _coerce_http_error(exc)
        if coerced is not None:
            status, resp = coerced
        elif _is_pre_send(exc):
            return (FAILED,
                    "transport failed before any bytes were sent (%s: %s)"
                    % (type(exc).__name__, exc))
        else:
            # Timeout, reset mid-stream, TLS failure after connect, ...
            # bytes may have reached the server.
            return (UNKNOWN,
                    "transport ambiguous (%s: %s) — the POST may have "
                    "reached the server; reconcile before any retry or "
                    "fallback submit" % (type(exc).__name__, exc))
    if status is None:
        return (UNKNOWN, "no HTTP status and no exception signal")
    if (isinstance(status, bool) or not isinstance(status, int)
            or not 100 <= status <= 599):
        return (UNKNOWN, "invalid HTTP status — reconcile before any retry")
    return _classify_status(status, {} if resp is None else resp, receipt)


def record_submission(receipt, attempt_id, role_id="", company="", ats="",
                      transport="", store_dir=None):
    """Record a SUBMITTED classification. Idempotent on receipt value:
    returns {"duplicate": True} when this receipt already counted — the
    caller must NOT re-announce, re-ledger, or re-record it.

    Only call after classify_attempt() returned SUBMITTED with a
    correlated receipt. Receipt structure and exact attempt identity are
    checked here as well; this is not independent provider verification.
    """
    if (not _correlated_receipt(receipt)
            or not isinstance(attempt_id, str) or not attempt_id.strip()
            or receipt.attempt_id != attempt_id):
        raise ValueError("refusing to record submission: a correlated receipt "
                         "for this exact attempt is required")
    rec = {"ts": _utcnow(), "attempt_id": attempt_id, "role_id": role_id,
           "company": company, "ats": ats, "transport": transport,
           "outcome": SUBMITTED, "receipt": receipt.to_dict(),
           "duplicate": False}
    # F17 race fix: the duplicate check AND the append happen under the
    # SAME fcntl lock. A read-then-append race would let two concurrent
    # attempts with the same receipt both count as submitted.
    with _LockedAppend(os.path.join(_store_dir(store_dir),
                                    OUTCOMES_LOG)) as fh:
        duplicate = False
        for row in _read_log(OUTCOMES_LOG, store_dir):
            r = row.get("receipt") or {}
            if (row.get("outcome") == SUBMITTED and not row.get("duplicate")
                    and r.get("value") == receipt.value):
                duplicate = True
                break
        rec["duplicate"] = duplicate
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
    return {"duplicate": duplicate, "record": rec}


def open_hold(attempt_id, role_id="", company="", ats="", transport="",
              reason="", receipt=None, page_ref=None, store_dir=None):
    """Open a durable reconciliation hold for an UNKNOWN attempt.

    Append-only and idempotent on attempt_id: a second open for the same
    attempt reuses the open hold instead of duplicating it. A hold is
    "open" until resolve_hold() records a resolution.

    page_ref carries non-sensitive ATS routing refs (submit_path,
    confirmation_path, board, job_id) so a later reconciliation pass —
    including the F22 submit_intent reconciler, which joins on
    attempt_id — can probe the provider without re-POSTing. Never
    stores answers, payloads, or secrets.
    """
    # F17 race fix: the open-check and the append share one fcntl lock so
    # two concurrent opens for the same attempt_id cannot double-append.
    with _LockedAppend(os.path.join(_store_dir(store_dir), HOLDS_LOG)) as fh:
        for hold in list_open_holds(store_dir=store_dir):
            if hold.get("attempt_id") == attempt_id:
                return {"reused": True, "hold": hold}
        hold = {
            "hold_id": "hold-" + hashlib.sha1(
                (attempt_id + _utcnow()).encode()).hexdigest()[:12],
            "attempt_id": attempt_id, "role_id": role_id, "company": company,
            "ats": ats, "transport": transport, "status": "open",
            "ts": _utcnow(), "reason": (reason or "")[:500],
            "receipt": receipt.to_dict() if receipt else None,
            "page_ref": page_ref or {},
        }
        fh.write(json.dumps(hold, sort_keys=True) + "\n")
    _maybe_auto_archive(store_dir)
    return {"reused": False, "hold": hold}


# Size-triggered self-maintenance (red-team 2026-09-17, rounds 1/6/8):
# the holds file must stay bounded even if no operator ever runs the
# archive manually. After each append, a single stat() checks the file
# size; past the threshold the resolved rows are archived under the
# same lock archive_resolved_holds() always uses. Best-effort: an
# archive failure never breaks the hold that was just opened.
_HOLD_AUTO_ARCHIVE_BYTES = 5 * 1024 * 1024


def _maybe_auto_archive(store_dir=None):
    try:
        path = os.path.join(_store_dir(store_dir), HOLDS_LOG)
        if os.path.getsize(path) > _HOLD_AUTO_ARCHIVE_BYTES:
            archive_resolved_holds(store_dir=store_dir)
    except OSError:
        pass


def resolve_hold(attempt_id, resolution, receipt=None, resolver="manual",
                 store_dir=None):
    """Record a resolution for an open hold (append-only — the original
    hold row is never mutated). Returns True when an open hold existed."""
    if not any(h.get("attempt_id") == attempt_id
               for h in list_open_holds(store_dir=store_dir)):
        return False
    row = {"attempt_id": attempt_id, "status": "resolved",
           "resolution": (resolution or "")[:500],
           "resolver": resolver, "ts": _utcnow(),
           "receipt": receipt.to_dict() if receipt else None}
    with _LockedAppend(os.path.join(_store_dir(store_dir), HOLDS_LOG)) as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    return True


def list_open_holds(store_dir=None):
    """All holds with no later resolution row, oldest first.

    Single pass over the log (red-team 2026-09-17 round 9): builds the
    resolved set and the open list from one read so the scan stays
    linear even as the file grows.
    """
    rows = _read_log(HOLDS_LOG, store_dir)
    resolved = {r.get("attempt_id") for r in rows
                if r.get("status") == "resolved"}
    return [r for r in rows
            if r.get("status") == "open" and r.get("hold_id")
            and r.get("attempt_id") not in resolved]


def archive_resolved_holds(store_dir=None):
    """Bound the hot holds file's growth (red-team finding 2026-09-17:
    append-only holds log with no rotation/pruning/archival).

    Moves terminal rows to an append-only archive file:
    submission-reconciliation-holds.jsonl.archive. A row is terminal
    when its attempt_id has a status=="resolved" row; both the resolved
    row and the open row(s) it closes are archived. Open holds are never
    touched. Both files stay append-only and the compaction is atomic
    (tmp + os.replace) under the same fcntl lock that guards appends,
    so a concurrent open_hold() cannot lose a row mid-compaction.

    This is a maintenance operation, not hot-path: call it from a cron
    or manually when the holds file grows large. Returns
    {"archived": <rows moved>}.
    """
    directory = _store_dir(store_dir)
    path = os.path.join(directory, HOLDS_LOG)
    archive_path = os.path.join(directory, HOLDS_LOG + ".archive")
    with _LockedAppend(path):
        rows = _read_log(HOLDS_LOG, store_dir)
        resolved_ids = {r.get("attempt_id") for r in rows
                        if r.get("status") == "resolved"}
        if not resolved_ids:
            return {"archived": 0}
        keep, drop = [], []
        for r in rows:
            (drop if r.get("attempt_id") in resolved_ids else keep).append(r)
        with open(archive_path, "a") as af:
            for r in drop:
                af.write(json.dumps(r, sort_keys=True) + "\n")
        tmp = path + ".tmp"
        with open(tmp, "w") as tf:
            for r in keep:
                tf.write(json.dumps(r, sort_keys=True) + "\n")
        os.replace(tmp, path)
        # NOTE: the _LockedAppend handle opened pre-replace is stale now;
        # it held only the lock, which is released on exit. Nothing was
        # written through it.
    return {"archived": len(drop)}


def main(argv):
    if "--list-holds" in argv:
        print(json.dumps(list_open_holds(), indent=1))
        return 0
    if "--archive" in argv:
        print(json.dumps(archive_resolved_holds()))
        return 0
    if "--resolve" in argv:
        i = argv.index("--resolve")
        try:
            attempt_id, resolution = argv[i + 1], argv[i + 2]
        except IndexError:
            print("usage: submission_outcome.py --resolve <attempt_id> "
                  "<resolution>")
            return 2
        ok = resolve_hold(attempt_id, resolution)
        print(json.dumps({"attempt_id": attempt_id, "resolved": ok}))
        return 0 if ok else 1
    print("usage: submission_outcome.py [--list-holds | --archive | "
          "--resolve <attempt_id> <resolution>]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
