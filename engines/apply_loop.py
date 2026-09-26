#!/usr/bin/env python3
"""Keel autonomous apply loop (public edition).

Picks the next eligible lead and builds a launch packet. This is the
open-core public half of the pipeline: discovery -> scoring -> materials ->
verification -> prescreen -> packet.

What it does NOT do (private execution layer, see SPLIT.md):
  - submit applications over HTTP (the API-direct transport lives in the
    private layer)
  - inject ATS-specific form-commit techniques into the brief (the private
    technique library)
  - spawn browser tasks (an agent-turn action using the packet)

Instead, each packet carries a generic brief — verified form values from the
answer bank, banded-question rules, hard gates, and the per-field
verification protocol — plus an EXECUTOR contract describing what the
submission layer must do and must never do. Plug in your own executor
(browser automation, ATS APIs, manual review) behind that contract.

Eligibility for autonomous application (standing approvals, no applicant input):
  - status READY (materials prepared) and action_band APPLY
  - employer not on the blocklist
  - employer application budget not exhausted (rate_limits.py)
  - no SUBMITTED ledger row for the same role_id, and no twin-submit
    (same normalized employer+title under a different role_id)
  - materials (resume) exist on disk
  - posting URL returns HTTP 200 (pre-launch live re-verify gate)
  - packet passes prescreen.py (PARK verdicts go to needs_input, never onward)
  - park-divergence tripwire: a needs_input-family gate_blocked NEWER than
    the queue's status_updated means the park write diverged from the queue
    (the system of record) — the lead is skipped, never selected

Ordering: highest fit_score first.

Packet buffer (prefetch): refresh_buffer() keeps BUFFER_MIN_FRESH fresh
launch packets warm for the top-ranked READY leads so the consumer claims
instead of waiting on a fresh build. Buffered packets live in
data/launch-packets/buffer/ (NOT the packets root) so the packet
watchdog's orphan scan — which only lists root-level files — never sees
them. Leads stay READY while buffered: refresh never writes IN-FLIGHT
markers, so in-flight slot accounting is never reset by a prefetch.

Second launch source: data/queues/strategic-queue.json. Strategic READY
leads compete alongside standard READY leads; all queue writes (parks,
IN-FLIGHT marks) go back to the owning file — one-lead-one-queue is
preserved. Strategic entries carry resume_version (a bare filename) instead
of a materials dict; the resumes/ path is derived transiently for the check
and never persisted to the entry.

Launch-lock guard: before a packet build claims a role_id, the loop tries
launch_lock.py's atomic acquire / --guard (duplicate/twin-submit guard).
A HELD/refused verdict skips the lead — the lane that owns it is already
on it. The acquired task_id is stored in the packet so the executor can
release the lock when the launch resolves.

Claim gate: a buffered packet is re-verified for liveness (live_cache TTL
when available, local 2h file cache otherwise) before the IN-FLIGHT mark.
DEAD -> the packet is dropped and a lead_dead gate is logged (fail closed:
never launch a dead posting). Unverifiable-over-HTTP -> proceeds with a
note; the executor re-verifies at Step 1.

Mapper plateau park: if the question mapper refuses the same form twice
with an IDENTICAL unmapped-question set AND the set contains a
genuine-input question, the lead parks to needs_input (a mapper plateau —
the mapper will not converge on its own). Never parks on the first
refusal, on a converging set, or on an ambiguous genuine-input match.
Wiring contract (ORDER MATTERS): FIRST call _maybe_plateau_park(entry,
unmapped) — it compares the current unmapped key against the PRIOR
refusal's stamp — THEN call _record_refusal(entry, unmapped, save_fn) to
replace the stamp. Recording first would compare the new key against
itself and park on the very first refusal.

What it does:
  1. Selects the highest-fit eligible lead (fit_score desc).
  2. Claims a fresh buffered packet, or builds one (form_intel.probe on
     the final ATS URL -> intel JSON, generic brief).
  3. Runs the live-cache claim gate on buffered packets.
  4. Marks the lead IN-FLIGHT so a second loop run cannot double-process.

After the executor reports, log the outcome with record_outcome.py and update
the ledger; the packet is then archived.

Usage:
    python3 apply_loop.py            # process one lead (highest fit)
    python3 apply_loop.py --all      # process every eligible lead, one packet each
    python3 apply_loop.py --refresh  # refill the packet buffer only, no claims

Env:
    KEEL_HOME  workspace root (default ~/keel)
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from keel_paths import HOME, DATA, TELEMETRY  # noqa: E402 — repo path convention
from safe_io import read_json  # noqa: E402 — bounded JSON reads

import form_intel
import log_event  # noqa: E402 — telemetry: additive event logging only
import prescreen  # noqa: E402 — pre-launch packet screen (PARK before spend)
import queue_io  # noqa: E402 — canonical locked queue writes (silent-defect sweep 2026-09-19)
import rate_limits  # noqa: E402 — employer application budgets
import record_outcome  # noqa: E402 — outcome telemetry

QUEUE = os.path.join(DATA, "queues", "standard-queue.json")
SQUEUE = os.path.join(DATA, "queues", "strategic-queue.json")
LEDGER = os.path.join(DATA, "application-ledger.json")
BLOCKLIST = os.path.join(DATA, "employer-blocklist.md")
PACKETS = os.path.join(DATA, "launch-packets")
STATE_DIR = os.path.join(DATA, ".state")
BUFFER_DIR = os.path.join(PACKETS, "buffer")
BUFFER_STATE = os.path.join(STATE_DIR, "packet-buffer-state.json")
BUFFER_WATERMARK = os.path.join(STATE_DIR, "packet-buffer-watermark.json")
LIVENESS_CACHE = os.path.join(STATE_DIR, "liveness-cache.json")
LAUNCH_LOCK_SCRIPT = os.path.join(BASE, "launch_lock.py")
UA = {"User-Agent": "Mozilla/5.0"}

PDT = ZoneInfo("America/Los_Angeles")
READY_STATES = ("READY", "READY-FOR-BROWSER")

# Packet buffer tuning. BUFFER_SIZE is the hard ceiling; BUFFER_MIN_FRESH is
# the just-in-time fill target (measured finding: filling to the ceiling
# every cycle overbuilds — most prefetched packets were never launched).
BUFFER_SIZE = 8
BUFFER_MIN_FRESH = 4
BUFFER_MAX_AGE_HOURS = 12
LIVE_CACHE_TTL_HOURS = 2
BUFFER_LOCK_FILE = os.path.join(STATE_DIR, "packet-buffer.lock")


# ---------------------------------------------------------------------------
# Queue IO
# ---------------------------------------------------------------------------

def load_answer_bank():
    # Prefer the personalized working copy in data/ (edited after setup.sh);
    # the engines/ copy is only a fallback for runs outside a workspace.
    data_bank = os.path.join(DATA, "answer_bank.json")
    if os.path.exists(data_bank):
        return json.load(open(data_bank))
    for name in ("answer_bank.json", "answer_bank.example.json"):
        p = os.path.join(BASE, name)
        if os.path.exists(p):
            return json.load(open(p))
    return {"answers": {}, "banded_questions": {}, "gates": {}}


def load_policy():
    # Keel Advance port (2026-09-26): pipeline_service.prepare_role calls
    # apply_loop.load_policy() — the live tree never had it, so prepare_role
    # raised AttributeError on any invocation (pre-existing on live main).
    return read_json(os.path.join(HOME, "data", "policy.json"))


def load_queue():
    try:
        with open(QUEUE) as queue_file:
            d = json.load(queue_file)
    except FileNotFoundError:
        sys.exit(
            f"{QUEUE} not found - run ./setup.sh first "
            "(or set KEEL_HOME to your workspace)"
        )
    return d if isinstance(d, list) else d.get("entries", d.get("items", []))


def load_strategic_queue():
    """Second launch source: strategic-queue.json entries. Missing file ->
    empty list (fail-open: the strategic lane is simply not active)."""
    try:
        d = json.load(open(SQUEUE))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return d if isinstance(d, list) else d.get("entries", d.get("items", []))


def _save_queue_file(path, items):
    # Canonical-path rule (silent-defect sweep 2026-09-19): every queue
    # write holds queue_io.queue_lock and goes through
    # queue_io.atomic_write_json (unique tmp + fsync + atomic rename).
    # The old unlocked read-modify-write with a fixed path+".tmp" name
    # let a stale snapshot silently clobber a canonical commit (IN-FLIGHT
    # reverted to PARKED-NEEDS-INPUT, reproduced 2026-09-19). The
    # list-vs-dict envelope shape is preserved; queue_lock is
    # reentrant-in-process so callers that already hold it are safe.
    with queue_io.queue_lock(owner="apply_loop:_save_queue_file"):
        d = json.load(open(path))
        if isinstance(d, list):
            d = items
        else:
            key = "entries" if "entries" in d else "items"
            d[key] = items
        queue_io.atomic_write_json(path, d)


def save_queue(items):
    _save_queue_file(QUEUE, items)


def save_strategic_queue(items):
    _save_queue_file(SQUEUE, items)


def mark_inflight(role_id, origin):
    """Mark one lead IN-FLIGHT in its OWNING queue file (one-lead-one-queue)."""
    if origin == "strategic":
        items, saver = load_strategic_queue(), save_strategic_queue
    else:
        items, saver = load_queue(), save_queue
    for e in items:
        if e.get("role_id") == role_id:
            e["status"] = "IN-FLIGHT"
            e["status_updated"] = datetime.now(PDT).strftime("%Y-%m-%d %H:%M PDT")
    saver(items)


def blocklisted(company):
    try:
        txt = open(BLOCKLIST).read().lower()
    except FileNotFoundError:
        return False
    return company.lower() in txt if company else False


# ---------------------------------------------------------------------------
# Posting-URL dedupe (tracking-query strip fix)
# ---------------------------------------------------------------------------

# Query parameters that are marketing/tracking noise. _posting_url strips
# ONLY these; every other query parameter is kept because on some boards
# the posting identity lives in the query string (e.g. ?go=listing&
# listingid=313979). Stripping the whole query collapsed every listing on
# such a board to one canonical URL and manufactured false "duplicate"
# verdicts.
_TRACKING_PARAMS = frozenset({
    "gclid", "gclsrc", "fbclid", "msclkid", "dclid", "igshid", "mc_cid",
    "mc_eid", "yclid", "vero_id", "vero_conv", "srsltid", "gad_source",
    "pk_campaign", "pk_kwd", "piwik_campaign", "matomo_campaign",
    "_ga", "_gl",
})
_TRACKING_PREFIXES = ("utm_", "utm")


def _strip_tracking_query(query):
    """Keep identity-bearing query params, drop tracking noise (sorted)."""
    kept = []
    for part in (query or "").split("&"):
        part = part.strip()
        if not part:
            continue
        key = part.split("=", 1)[0].strip().lower()
        if key in _TRACKING_PARAMS or key.startswith(_TRACKING_PREFIXES):
            continue
        kept.append(part.lower())
    return "&".join(sorted(kept))


def _posting_url(u):
    """Canonical posting-URL form for role-level dedupe.

    _posting_url plus stripping of ATS confirmation/application suffixes:
    ledger rows usually carry the confirmation URL
    (greenhouse.io/<board>/jobs/<id>/confirmation,
    ashbyhq.com/<org>/<uuid>/application), which embeds the same posting
    identity as the candidate's bare posting URL. Query strings are
    preserved except for tracking parameters (see _TRACKING_PARAMS).
    """
    raw = (u or "").strip()
    nofrag = raw.split("#", 1)[0]
    path_part, _, query = nofrag.partition("?")
    n = path_part.strip().lower().rstrip("/")
    for suffix in ("/confirmation", "/application"):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
            break
    q = _strip_tracking_query(query)
    return n + ("?" + q if q else "")


def _norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "",
                                      (s or "").lower())).strip()


def already_submitted(company, title, role_id=None):
    """True when the ledger says this role is already applied.

    Three independent checks (any hit blocks):
      1. the existing employer-level check (same company, any SUBMITTED row)
      2. exact role_id match on a SUBMITTED row
      3. twin-submit guard: same normalized company+title under a DIFFERENT
         role_id — the duplicate-application class the launch-lock --guard
         closes at spawn time (fail closed: HOLD/skip for adjudication)
    """
    try:
        rows = json.load(open(LEDGER))
    except FileNotFoundError:
        return False
    rows = rows if isinstance(rows, list) else rows.get("rows", [])
    c = (company or "").lower()
    t = _norm(title)
    for r in rows:
        if str(r.get("status", "")).upper() != "SUBMITTED":
            continue
        if c and c in (r.get("company") or "").lower():
            return True
        if role_id and r.get("role_id") == role_id:
            return True
        rt = _norm(r.get("role_title") or r.get("title"))
        rc = _norm(r.get("company"))
        if c and t and rt and _norm(c) == rc and t == rt \
                and r.get("role_id") != role_id:
            return True  # twin-submit: same employer+role, different role_id
    return False


# ---------------------------------------------------------------------------
# Liveness (live-cache claim gate)
# ---------------------------------------------------------------------------

def live(url):
    """Tri-state liveness check.

    True  -> HTTP 200, posting live.
    False -> HTTP 404/410, posting dead.
    None  -> unverifiable over plain HTTP (bot protection, timeout, 403).
            Never treated as dead — the executor re-verifies before filing.
    """
    import urllib.error
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        return False if e.code in (404, 410) else None
    except Exception:
        return None


def _liveness_local_load():
    try:
        d = json.load(open(LIVENESS_CACHE))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return d if isinstance(d, dict) else {}


def _liveness_local_get(role_id, url):
    d = _liveness_local_load()
    rec = d.get(role_id) or {}
    if rec.get("url") != url or rec.get("verdict") != "live":
        return False
    try:
        ts = datetime.fromisoformat(rec.get("ts", ""))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    return datetime.now(timezone.utc) - ts <= timedelta(hours=LIVE_CACHE_TTL_HOURS)


def _liveness_local_record(role_id, url, verdict):
    try:
        d = _liveness_local_load()
        d[role_id] = {"url": url, "verdict": verdict,
                      "ts": datetime.now(timezone.utc).isoformat()}
        os.makedirs(os.path.dirname(LIVENESS_CACHE), exist_ok=True)
        json.dump(d, open(LIVENESS_CACHE, "w"), indent=1)
    except OSError:
        pass


def live_cached(role_id, url):
    """TTL-cached liveness. Same tri-state as live().

    Skips the HTTP round-trip when a fresh (<=2h) 'live' verdict exists for
    this role_id + URL — the duplicate check between promotion-time
    verification and the claim-time re-verify gate. Prefers the live_cache
    engine when it is on sys.path; falls back to a local 2h file cache.
    Fail-closed: any cache doubt falls through to the real HTTP check, so
    a broken or stale cache can only cost a request, never skip verification.
    """
    try:
        import live_cache as _lc  # noqa — canonical liveness-verdict cache
        if _lc.fresh_live(role_id, url):
            return True
        cache_ok = True
    except Exception:
        cache_ok = False
        if _liveness_local_get(role_id, url):
            return True
    verdict = live(url)
    try:
        if cache_ok:
            import live_cache as _lc2  # noqa
            _lc2.record(role_id, url,
                        {True: "live", False: "dead"}.get(verdict, "ambiguous"))
        else:
            _liveness_local_record(
                role_id, url,
                {True: "live", False: "dead"}.get(verdict, "ambiguous"))
    except Exception:
        pass
    return verdict


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------

def _materials_for(entry, origin="standard"):
    """Resolve the materials dict for an entry.

    Strategic-queue entries carry resume_version (a bare filename) instead
    of a materials dict. The resumes/ path is derived transiently — never
    persisted to the entry.
    """
    m = entry.get("materials")
    if isinstance(m, dict):
        return m
    if origin == "strategic":
        rv = entry.get("resume_version")
        if rv and isinstance(rv, str):
            return {"resume": "resumes/" + rv}
    return {}


def materials_ok(entry, origin="standard"):
    m = _materials_for(entry, origin)
    rp = m.get("resume")
    if not rp:
        return False
    return os.path.exists(os.path.join(HOME, rp))


# ---------------------------------------------------------------------------
# Park-divergence tripwire
# ---------------------------------------------------------------------------

def _park_family_gates():
    try:
        return log_event.PARK_FAMILY_GATES
    except AttributeError:
        return frozenset({"needs_input"})


_tripwire_cache = None
_tripwire_mtime = None


def _tripwire_blocked_map():
    """Latest needs_input-family gate_blocked timestamp per role_id.

    Scanned once per events-file mtime and cached; fail-open on telemetry
    read errors (empty map — the tripwire simply does not fire) so a bad
    log can never starve the lane.
    """
    global _tripwire_cache, _tripwire_mtime
    path = log_event.EVENTS
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _tripwire_cache is not None and _tripwire_mtime == mtime:
        return _tripwire_cache
    blocked = {}
    try:
        with open(path) as f:
            for line in f:
                if "gate_blocked" not in line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("event_type") != "gate_blocked":
                    continue
                if (ev.get("details") or {}).get("gate") not in _park_family_gates():
                    continue
                rid = ev.get("role_id")
                ts = ev.get("ts")
                if rid and ts and (rid not in blocked or ts > blocked[rid]):
                    blocked[rid] = ts
    except (FileNotFoundError, OSError):
        return _tripwire_cache or {}
    _tripwire_cache = blocked
    _tripwire_mtime = mtime
    return blocked


def _parse_status_ts(raw):
    """Parse a queue status_updated stamp to an aware datetime.

    Queue stamps look like '2026-09-15 20:11 PDT' (America/Los_Angeles) or
    '2026-09-16 03:35 UTC' (some writers stamp UTC). Returns None when
    unparseable (fail-open: the tripwire does not fire on a bad stamp).
    """
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    tz = PDT
    for label, zone in (("PDT", PDT), ("PST", PDT), ("UTC", timezone.utc)):
        if text.endswith(" " + label):
            text = text[: -len(label)].strip()
            tz = zone
            break
    try:
        dt = datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return dt.replace(tzinfo=tz)


def _tripwire_skip(entry):
    """True when the park-divergence tripwire should skip this entry.

    A READY lead whose telemetry carries a needs_input-family gate_blocked
    NEWER than its queue status_updated is skipped: the park write diverged
    from the queue and the queue is the system of record — never select a
    lead whose latest park signal the queue does not reflect.
    """
    bts = _tripwire_blocked_map().get(entry.get("role_id"))
    if not bts:
        return False
    try:
        b_dt = datetime.fromisoformat(bts)
    except (ValueError, TypeError):
        return False
    if b_dt.tzinfo is None:
        b_dt = b_dt.replace(tzinfo=timezone.utc)
    su_dt = _parse_status_ts(entry.get("status_updated"))
    # A missing/older queue stamp with a newer park signal = divergence.
    return su_dt is None or b_dt > su_dt


# ---------------------------------------------------------------------------
# Launch-lock guard
# ---------------------------------------------------------------------------

def _launch_guard(role_id, company, title):
    """Pre-build duplicate/twin-submit guard via launch_lock (atomic).

    Returns (go: bool, task_id: str, reason: str). On GO the lock is HELD by
    this task_id — store it in the packet so the executor can release it
    (or it expires). Fail-open when the launch_lock tooling is missing:
    the twin-submit check in already_submitted() still guards ledger-level
    duplicates. A HELD/refused verdict skips the lead — the owning lane is
    already on it.
    """
    task_id = "apply_loop-%d-%s" % (
        os.getpid(), datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"))
    try:
        import launch_lock as _ll  # noqa — atomic per-role lock + --guard
        try:
            if hasattr(_ll, "prelaunch_guard"):
                # Returns (ok: bool, info: dict) with info["verdict"] one of
                # GO / REFUSE / STAND_DOWN — parse it explicitly; a bare
                # truthiness check on the tuple would read every refusal
                # (a non-empty tuple) as GO.
                ok, info = _ll.prelaunch_guard(
                    role_id, task_id,
                    company=company or "", title=title or "",
                    owner="apply_loop")
                info = info or {}
                verdict = str(info.get("verdict", "")).upper()
                status = info.get("status", "")
                if ok and verdict == "GO":
                    return True, task_id, ""
                return False, task_id, (
                    f"prelaunch guard {status or verdict or 'refused'}: "
                    f"{info.get('note', '')}".strip())
            ok, info = _ll.acquire(role_id, task_id, owner="apply_loop")
            if ok:
                return True, task_id, ""
            return (False, task_id,
                    "launch lock HELD: %s" % ((info or {}).get("status", "")))
        except ValueError:
            # Corrupt lease (K20): never absorbed into a fail-open
            # "proceeding" — the ValueError must propagate loudly for
            # operator reconciliation (silent-defect sweep 2026-09-19).
            raise
        except Exception as ex:
            return True, task_id, f"guard tooling failed ({ex}); proceeding"
    except ImportError:
        pass
    # CLI fallback: the sibling launch_lock.py module may exist without being
    # imported (or vice versa).
    if os.path.isfile(LAUNCH_LOCK_SCRIPT):
        try:
            r = subprocess.run(
                [sys.executable, LAUNCH_LOCK_SCRIPT, "--guard", role_id,
                 task_id, "--company", company or "", "--title", title or ""],
                capture_output=True, text=True, timeout=30)
            out = (r.stdout or "") + (r.stderr or "")
            if r.returncode == 0 and re.search(r"\bGO\b", out):
                return True, task_id, ""
            return False, task_id, (
                out.strip().splitlines()[-1] if out.strip() else "guard refused")
        except Exception as ex:
            return True, task_id, f"guard tooling failed ({ex}); proceeding"
    return True, task_id, "no launch_lock tooling; guard skipped (fail-open)"


def _release_launch_lock(role_id, task_id):
    """Release a lock we acquired but no longer need (parked, dead, or failed).

    Best-effort: failures are logged, never fatal. Never raises.
    """
    if not role_id or not task_id:
        return
    try:
        import launch_lock as _ll  # noqa
        _ll.release(role_id, task_id)
        return
    except Exception:
        pass
    if os.path.isfile(LAUNCH_LOCK_SCRIPT):
        try:
            subprocess.run([sys.executable, LAUNCH_LOCK_SCRIPT, "--release",
                            role_id, task_id],
                           capture_output=True, text=True, timeout=30)
        except Exception as ex:
            print(f"  warn: lock release failed for {role_id}: {ex}",
                  file=sys.stderr)


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def eligible(entry, origin="standard"):
    if entry.get("status") not in READY_STATES:
        return False, "not READY"
    # Park-divergence tripwire: needs_input-family gate_blocked newer than
    # the queue status_updated means the park write diverged — never select.
    if _tripwire_skip(entry):
        return False, ("park-divergence tripwire: needs_input-family "
                       "gate_blocked newer than queue status_updated")
    # Strategic-queue entries don't carry the standard queue's action_band
    # classifier vocabulary. They are curated verified-live leads, so a
    # missing or STRATEGIC-prefixed band is the strategic equivalent of
    # APPLY. Every other gate below (dedupe, materials, liveness, blocklist)
    # still applies unchanged.
    band = entry.get("action_band")
    if origin == "strategic":
        band_ok = (band in (None, "APPLY")
                   or str(band).startswith("STRATEGIC"))
    else:
        band_ok = (band == "APPLY")
    if not band_ok:
        return False, f"not APPLY band ({band})"
    if blocklisted(entry.get("company")):
        return False, "blocklisted employer"
    if already_submitted(entry.get("company"), entry.get("title"),
                         entry.get("role_id")):
        return False, "already submitted (role_id, twin, or employer match)"
    if not materials_ok(entry, origin):
        return False, "materials not prepared"
    url = entry.get("ats_url") or entry.get("application_url")
    if not url:
        return False, "no application URL"
    lv = live(url)
    if lv is False:
        return False, "posting dead (404/410)"
    if lv is None:
        print(f"  note: {entry['role_id']} unverifiable over HTTP "
              f"— executor re-verifies")
    return True, "ok"


# ---------------------------------------------------------------------------
# Mapper plateau park
# ---------------------------------------------------------------------------

# Genuine-input question classes for plateau adjudication. A question is
# genuine-input ONLY when it matches one of these exact patterns —
# fail closed: anything ambiguous does not park.
_PLATEAU_GENUINE_PATTERNS = (
    # "Which AI tools [or platforms] have you worked with?" — the answer
    # bank has no tool-set key, so any tool list typed or selected would
    # be invented. An applicant-confirmed tool-set key would un-plateau.
    re.compile(r"\bwhich ai tools\b.{0,50}\b(have you worked with|"
               r"have experience)\b", re.I),
    # "Are you interested in an individual contributor role?" — a
    # role-level employment-trajectory decision only the applicant can make.
    re.compile(r"\binterested in\b.{0,40}\bindividual contributor\b", re.I),
)


def _plateau_genuine(question_text):
    """True only when question_text matches an exact genuine-input pattern."""
    text = " ".join(str(question_text or "").split())
    return any(p.search(text) for p in _PLATEAU_GENUINE_PATTERNS)


def _unmapped_key(unmapped):
    """Stable, order-insensitive JSON-safe key for a refusal's unmapped set."""
    texts = []
    for q in unmapped or []:
        t = q.get("question_text") if isinstance(q, dict) else q
        texts.append(" ".join(str(t or "").split()).lower())
    return sorted(texts)


def _record_refusal(entry, unmapped):
    """Stamp the refusal's unmapped-set key on the queue entry (in memory).

    Call AFTER _maybe_plateau_park on every question-mapper refusal — the
    plateau comparison must run against the PRIOR stamp, before this
    replaces it. The caller persists the queue.
    """
    entry["last_refusal_unmapped"] = _unmapped_key(unmapped)


def _maybe_plateau_park(entry, unmapped):
    """Plateau-park adjudication.

    MUST be called BEFORE _record_refusal replaces the prior stamp: it
    compares the current unmapped key against the previous refusal's
    stamp. Returns True and parks the lead to needs_input via the
    sanctioned prescreen.park_lead() path when the unmapped set is
    IDENTICAL across 2 consecutive refusals AND contains >=1 genuine-input
    question (a mapper plateau — the mapper will not converge on its own).
    Never parks on the first refusal, on a converging set, or on an
    ambiguous genuine-input classification — fail closed in all three
    cases.
    """
    role_id = entry.get("role_id", "")
    key = _unmapped_key(unmapped)
    if not key:
        return False  # nothing unmapped — no plateau to park on
    prev = entry.get("last_refusal_unmapped") or []
    if list(prev) != key:
        return False  # first refusal, or the mapper is converging
    genuine = []
    for q in (unmapped or []):
        text = q.get("question_text") if isinstance(q, dict) else q
        if _plateau_genuine(text):
            genuine.append(" ".join(str(text or "").split())[:200])
    if not genuine:
        return False  # fail closed: no exact genuine-input match
    reasons = [
        "question-mapper plateau: unmapped set identical across "
        "2 consecutive refusals",
    ] + [f"genuine input needed — \"{g}\"" for g in genuine]
    res = prescreen.park_lead(role_id, reasons)
    if res.get("ok"):
        print(f"PLATEAU-PARK {role_id}: parked needs_input on mapper "
              f"plateau ({len(genuine)} genuine-input question(s))")
        return True
    print(f"PLATEAU-PARK-FAILED {role_id}: {res.get('error')}")
    return False


# ---------------------------------------------------------------------------
# Packet buffer (prefetch)
# ---------------------------------------------------------------------------

def load_buffer_state():
    """Buffer state: list of {role_id, packet_path, built_at, fit_score,
    origin, launch_task_id}. Missing/corrupt file -> empty buffer
    (fail-open, the refresh rebuilds it)."""
    try:
        with open(BUFFER_STATE) as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def save_buffer_state(state):
    os.makedirs(os.path.dirname(BUFFER_STATE), exist_ok=True)
    tmp = BUFFER_STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, BUFFER_STATE)


def _queue_content_hash(tagged):
    """Content hash of the parsed queues (not mtime): the refresh rewrites
    queue files even when the reconcile changed nothing, which would defeat
    an mtime-based watermark. Hashing parsed content lets the refresh skip
    saving when nothing changed, preserving the watermark across idle cycles."""
    import hashlib
    return hashlib.sha256(
        json.dumps(tagged, sort_keys=True, default=str).encode()
    ).hexdigest()


def _load_buffer_watermark():
    try:
        with open(BUFFER_WATERMARK) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_buffer_watermark(queue_hash):
    os.makedirs(os.path.dirname(BUFFER_WATERMARK), exist_ok=True)
    json.dump({"queue_hash": queue_hash,
               "cycle_at": datetime.now(timezone.utc).isoformat()},
              open(BUFFER_WATERMARK, "w"), indent=1)


def _buffer_lock():
    """Non-blocking process lock so two refresh runs can never interleave
    buffer-state/queue writes. Returns the lock handle, or None when another
    run holds it (the loser exits quietly — the winner's state is
    authoritative)."""
    try:
        import fcntl
    except ImportError:
        return True  # no flock: single-process assumption, proceed
    os.makedirs(STATE_DIR, exist_ok=True)
    fh = open(BUFFER_LOCK_FILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        fh.close()
        return None
    return fh


def _buffer_fresh_count(state, now=None):
    """Number of buffer-state entries that are fresh: packet file present
    AND age <= BUFFER_MAX_AGE_HOURS. Fail-open per entry: a malformed or
    unreadable entry counts as not-fresh, never as fresh."""
    now = now or datetime.now(timezone.utc)
    n = 0
    for s in state or []:
        pkt = s.get("packet_path")
        if not (pkt and os.path.isfile(pkt)):
            continue
        try:
            built = datetime.fromisoformat(s.get("built_at", ""))
            if built.tzinfo is None:
                built = built.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if (now - built).total_seconds() / 3600 <= BUFFER_MAX_AGE_HOURS:
            n += 1
    return n


def _buffered_fresh_packet(role_id, now=None):
    """Path of a fresh buffered packet for role_id, else None."""
    now = now or datetime.now(timezone.utc)
    for s in load_buffer_state():
        if s.get("role_id") != role_id:
            continue
        pkt = s.get("packet_path")
        if not (pkt and os.path.isfile(pkt)):
            continue
        try:
            built = datetime.fromisoformat(s.get("built_at", ""))
            if built.tzinfo is None:
                built = built.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if (now - built).total_seconds() / 3600 <= BUFFER_MAX_AGE_HOURS:
            return pkt
    return None


def _drop_buffer_entry(role_id):
    save_buffer_state([s for s in load_buffer_state()
                       if s.get("role_id") != role_id])


def refresh_buffer(tagged=None, now=None):
    """Prefetch launch packets for the top-ranked eligible leads.

    Keeps BUFFER_MIN_FRESH fresh packets warm (BUFFER_SIZE hard ceiling).
    Skips the whole pass when the queue content matches the last cycle's
    watermark AND the buffer is already full of fresh, present packets.
    Leads stay READY while buffered — no IN-FLIGHT markers are written.
    Returns the number of fresh packets on hand.
    """
    now = now or datetime.now(timezone.utc)
    lock = _buffer_lock()
    if lock is None:
        print("refresh_buffer: another refresh holds the lock; skipping")
        return _buffer_fresh_count(load_buffer_state(), now)
    try:
        if tagged is None:
            tagged = ([(e, "standard") for e in load_queue()]
                      + [(e, "strategic") for e in load_strategic_queue()])
        state = load_buffer_state()

        # Reconcile: drop entries whose lead left READY, whose packet file
        # is gone, or whose packet aged out (stale packets are archived).
        by_id = {e.get("role_id"): e for e, _ in tagged if e.get("role_id")}
        fresh_state = []
        for s in state:
            rid = s.get("role_id")
            pkt = s.get("packet_path")
            stale = True
            try:
                built = datetime.fromisoformat(s.get("built_at", ""))
                if built.tzinfo is None:
                    built = built.replace(tzinfo=timezone.utc)
                stale = (now - built).total_seconds() / 3600 > BUFFER_MAX_AGE_HOURS
            except (ValueError, TypeError):
                pass
            e = by_id.get(rid)
            if (e is None or e.get("status") not in READY_STATES
                    or not (pkt and os.path.isfile(pkt)) or stale):
                if pkt and os.path.isfile(pkt):
                    _archive_packet(pkt)
                continue
            fresh_state.append(s)
        state = fresh_state
        fresh = _buffer_fresh_count(state, now)

        # Refresh-skip watermark: identical queue content + full fresh
        # buffer means there is nothing to do.
        qhash = _queue_content_hash(tagged)
        if fresh >= BUFFER_MIN_FRESH and \
                _load_buffer_watermark().get("queue_hash") == qhash:
            save_buffer_state(state)
            return fresh

        buffered_ids = {s.get("role_id") for s in state}
        candidates = [
            (e, origin) for e, origin in tagged
            if e.get("status") in READY_STATES
            and e.get("role_id") not in buffered_ids
        ]
        candidates.sort(key=lambda p: _eff_score(p[0]), reverse=True)
        need = max(0, BUFFER_MIN_FRESH - fresh)
        room = max(0, BUFFER_SIZE - len(state))
        bank = load_answer_bank()
        built = 0
        for entry, origin in candidates[:max(need, 0)]:
            if built >= room:
                break
            role_id = entry.get("role_id", "")
            ok, why = eligible(entry, origin)
            if not ok:
                continue
            go, task_id, greason = _launch_guard(
                role_id, entry.get("company"), entry.get("title"))
            if not go:
                print(f"BUFFER-SKIP {role_id}: {greason}")
                continue
            path = build_packet(entry, origin=origin, dest_dir=BUFFER_DIR,
                                task_id=task_id)
            packet = json.load(open(path))
            try:
                verdict = prescreen.screen_packet(packet, bank)
            except Exception as ex:
                print(f"  prescreen error on {role_id} ({ex}); treating as CLEAN")
                verdict = {"verdict": "CLEAN", "reasons": []}
            if verdict["verdict"] == "PARK":
                res = prescreen.park_lead(role_id, verdict["reasons"])
                _release_launch_lock(role_id, task_id)
                os.path.exists(path) and os.remove(path)
                print(f"BUFFER-PARK {role_id}: "
                      f"{'parked' if res.get('ok') else res.get('error')}")
                continue
            state.append({
                "role_id": role_id,
                "packet_path": path,
                "built_at": now.isoformat(),
                "fit_score": entry.get("fit_score"),
                "origin": origin,
                "launch_task_id": task_id,
            })
            built += 1
            log_event.log(
                "brief_built", role_id=role_id,
                company=entry.get("company", ""), ats="",
                source="apply_loop",
                details={"packet": path, "fit_score": entry.get("fit_score"),
                         "buffered": True},
            )
            print(f"BUFFERED {role_id} -> {path}")
        save_buffer_state(state)
        _save_buffer_watermark(qhash)
        return _buffer_fresh_count(state, now)
    finally:
        try:
            lock.close()
        except Exception:
            pass


def _buffered_launch_task_id(role_id):
    """The task_id that owns the launch lock for a buffered packet.

    Read BEFORE _claim_buffered: on the claim-dead path _claim_buffered
    deletes the packet file carrying launch_task_id, which would destroy
    the owner id needed to release the lock (silent 2h lock leak —
    silent-defect sweep 2026-09-19)."""
    for s in load_buffer_state():
        if s.get("role_id") == role_id:
            return s.get("launch_task_id") or ""
    return ""


def _claim_or_release(entry, bpath, role_id):
    """Claim a buffered packet; on the claim-dead path release the
    buffer-time launch lock and return None.

    _claim_buffered deletes the packet file (which carries launch_task_id)
    on the DEAD path, so the owner id is read from the buffer state BEFORE
    the claim — otherwise the lock leaks for the full 2h TTL
    (silent-defect sweep 2026-09-19). _release_launch_lock is a no-op when
    the task_id is unknown."""
    claim_task_id = _buffered_launch_task_id(role_id)
    path = _claim_buffered(entry, bpath)
    if not path:
        # claim-dead: posting dead, packet dropped — release the
        # buffer-time launch lock.
        _release_launch_lock(role_id, claim_task_id)
    return path


def _claim_buffered(entry, bpath):
    """Claim a fresh buffered packet: move it to the packets root and run
    the live-cache claim gate. DEAD -> drop the packet, log lead_dead, and
    return None (fail closed: never launch a dead posting)."""
    role_id = entry.get("role_id", "")
    dest = os.path.join(PACKETS, os.path.basename(bpath))
    os.makedirs(PACKETS, exist_ok=True)
    os.replace(bpath, dest)
    _drop_buffer_entry(role_id)
    url = entry.get("ats_url") or entry.get("application_url")
    lv = live_cached(role_id, url)
    if lv is False:
        log_event.log(
            "gate_blocked", role_id=role_id,
            company=entry.get("company", ""), ats="",
            source="apply_loop",
            details={"gate": "lead_dead",
                     "reason": "claim-time re-verify: posting dead (404/410)"},
        )
        os.path.exists(dest) and os.remove(dest)
        print(f"CLAIM-DEAD {role_id}: packet dropped, posting dead")
        return None
    if lv is None:
        print(f"  note: {role_id} unverifiable over HTTP — executor re-verifies")
    print(f"CLAIMED {role_id} -> {dest}")
    return dest


# ---------------------------------------------------------------------------
# Packet building
# ---------------------------------------------------------------------------

def build_generic_brief(entry, intel, bank):
    """Build the executor brief WITHOUT private commit techniques.

    The brief carries: verified form values, banded-question rules, hard
    gates, form intel (exact option labels when available), and the
    per-field verification protocol. ATS-specific event-sequencing
    techniques are the private layer's job (see SPLIT.md).
    """
    answers = bank.get("answers", {})
    lines = [
        f"Submit a job application for the applicant to {entry.get('company')} "
        f"for the role \"{entry.get('title')}\".",
        "",
        "STEP 1 — RE-VERIFY LIVE. Confirm the posting is live and accepting "
        "applications BEFORE filling anything. If the posting is gone, expired, "
        "or the apply route is gated/paywalled, STOP and report — do not submit.",
        f"Application URL: {entry.get('ats_url') or entry.get('application_url')}",
        "",
        "STEP 2 — PRE-FLIGHT INTELLIGENCE. Enumerate every question and its "
        "exact option labels from the rendered form first; use those exact labels.",
    ]
    if intel:
        lines.append(f"Detected ATS: {intel.get('ats', 'unknown')}")
    lines += [
        "",
        "Fill the form with these truthful values (never invent anything else):",
    ]
    for k, v in answers.items():
        lines.append(f"  - {k}: {v}")
    lines += ["", "BANDED-QUESTION RULES (pre-approved — apply without improvising):"]
    for k, rule in bank.get("banded_questions", {}).items():
        r = rule.get("rule", rule) if isinstance(rule, dict) else rule
        lines.append(f"  - {k}: {r}")
    lines += ["", "HARD GATES (stop conditions — obey exactly):"]
    for k, gate in bank.get("gates", {}).items():
        lines.append(f"  - {k}: {gate}")
    lines += [
        "",
        "STEP 3 — PER-FIELD VERIFICATION PROTOCOL (mandatory, after EVERY field):",
        "  - After EVERY field fill, verify commitment before moving on: no validation",
        "    error, and the entered value must persist after clicking elsewhere (blur test).",
        "  - For dropdowns: reopen the menu and confirm the option carries a selected marker.",
        "  - Never batch-fill then check at the end. One field, one verification, then next.",
        "  - Only when all required fields verify clean may Submit be clicked.",
        "",
        "STEP 4 — SUBMIT only when every required field verifies clean. If an email/phone",
        "verification code screen appears, PAUSE and hand off — do not guess codes.",
        "Report the EXACT confirmation page text/message. Explicit confirmation is the",
        "only acceptable success evidence.",
        "",
        "EXECUTOR CONTRACT (private layer implements this):",
        "  - MUST re-verify the posting is live before filling (Step 1).",
        "  - MUST NOT invent values for fields not covered above — ask instead.",
        "  - MUST NOT attest to anything on the applicant's behalf (no-AI, travel,",
        "    arbitration, references) without explicit applicant approval.",
        "  - MUST NOT claim submission without explicit confirmation evidence.",
        "  - MUST respect employer rate limits and the blocklist.",
    ]
    return "\n".join(lines)


def build_packet(entry, origin="standard", dest_dir=None, task_id=""):
    """Build a launch packet. dest_dir defaults to the packets root;
    the buffer refresh passes BUFFER_DIR."""
    role_id = entry["role_id"]
    url = entry.get("ats_url") or entry.get("application_url")
    try:
        intel = form_intel.probe_url(url)
        intel["role_id"] = role_id
        intel["source_url"] = url
        intel_path = os.path.join(BASE, "briefs", f"{role_id}.intel.json")
        os.makedirs(os.path.dirname(intel_path), exist_ok=True)
        json.dump(intel, open(intel_path, "w"), indent=2)
        ats = intel.get("ats")
    except Exception as ex:
        print(f"  intel failed for {role_id} ({ex}); continuing without it")
        intel, ats = None, "unknown"
    bank = load_answer_bank()
    brief = build_generic_brief(entry, intel, bank)
    m = _materials_for(entry, origin)
    packet = {
        "role_id": role_id,
        "company": entry.get("company"),
        "title": entry.get("title"),
        "ats_url": url,
        "ats": ats,
        "upload_files": [os.path.join(HOME, m["resume"])] +
                        ([os.path.join(HOME, m["cover_letter"])] if m.get("cover_letter") else []),
        "brief_chars": len(brief),
        "brief": brief,
        "executor": "pluggable — implement the EXECUTOR CONTRACT in the brief",
    }
    if task_id:
        packet["launch_task_id"] = task_id
    dest = dest_dir or PACKETS
    os.makedirs(dest, exist_ok=True)
    path = os.path.join(dest, f"{role_id}.json")
    json.dump(packet, open(path, "w"), indent=2)
    return path


def _eff_score(e):
    f = e.get("fit_score")
    return f if isinstance(f, (int, float)) else -1


def _archive_packet(path):
    import shutil
    adir = os.path.join(PACKETS, "archive")
    os.makedirs(adir, exist_ok=True)
    if path and os.path.exists(path):
        shutil.move(path, os.path.join(adir, os.path.basename(path)))


def main():
    items = load_queue()
    sitems = load_strategic_queue()
    tagged = [(e, "standard") for e in items] + [(e, "strategic") for e in sitems]
    if "--refresh" in sys.argv:
        fresh = refresh_buffer(tagged)
        print(f"Buffer refresh done: {fresh} fresh packet(s) on hand.")
        return
    bank = load_answer_bank()
    todo = sorted(
        (p for p in tagged if p[0].get("status") in READY_STATES),
        key=lambda p: _eff_score(p[0]),
        reverse=True)
    if not todo:
        print("No READY leads. Nothing to do.")
        return
    inflight_ids = []
    made = 0
    for entry, origin in todo:
        role_id = entry.get("role_id", "")
        company = entry.get("company", "")
        ok, why = eligible(entry, origin)
        if not ok:
            log_event.log(
                "gate_blocked", role_id=role_id, company=company, ats="",
                source="apply_loop",
                details={"gate": "eligibility", "reason": why,
                         "fit_score": entry.get("fit_score"),
                         "origin": origin},
            )
            print(f"SKIP {role_id}: {why}")
            continue
        allowed, rl_reason = rate_limits.is_allowed(company)
        if not allowed:
            log_event.log(
                "gate_blocked", role_id=role_id, company=company, ats="",
                source="apply_loop",
                details={"gate": "rate_limit", "reason": rl_reason},
            )
            print(f"SKIP {role_id}: {rl_reason}")
            continue
        # Prefer a fresh buffered packet over a fresh build (claim path).
        bpath = _buffered_fresh_packet(role_id)
        task_id = ""
        if bpath:
            path = _claim_or_release(entry, bpath, role_id)
            if not path:
                continue  # claim-dead: posting dead, packet dropped, lock released
        else:
            go, task_id, greason = _launch_guard(
                role_id, company, entry.get("title"))
            if not go:
                log_event.log(
                    "gate_blocked", role_id=role_id, company=company, ats="",
                    source="apply_loop",
                    details={"gate": "launch_lock", "reason": greason},
                )
                print(f"SKIP {role_id}: {greason}")
                continue
            if greason:
                print(f"  note: {role_id} {greason}")
            path = build_packet(entry, origin=origin, task_id=task_id)
        packet = json.load(open(path))
        if not task_id:
            # Buffered-claim path: the launch lock was acquired at
            # buffer-build time; recover its task_id from the packet so a
            # later PARK can release the lock instead of leaking it.
            task_id = packet.get("launch_task_id", "") or ""
        try:
            verdict = prescreen.screen_packet(packet, bank)
        except Exception as ex:
            print(f"  prescreen error on {role_id} ({ex}); treating as CLEAN")
            verdict = {"verdict": "CLEAN", "reasons": []}
        if verdict["verdict"] == "PARK":
            res = prescreen.park_lead(role_id, verdict["reasons"])
            _release_launch_lock(role_id, task_id)
            if res.get("ok"):
                first = verdict["reasons"][0] if verdict["reasons"] else "blocked"
                print(f"PRESCREEN-PARK {role_id}: {first}")
            else:
                print(f"PRESCREEN-PARK-FAILED {role_id}: {res.get('error')}")
            made += 1
            if "--all" not in sys.argv:
                break
            continue
        inflight_ids.append((role_id, origin))
        log_event.log(
            "lead_verified", role_id=role_id, company=company, ats="",
            source="apply_loop",
            details={"url": entry.get("ats_url") or entry.get("application_url", ""),
                     "origin": origin},
        )
        log_event.log(
            "brief_built", role_id=role_id, company=company, ats="",
            source="apply_loop",
            details={"packet": path, "fit_score": entry.get("fit_score")},
        )
        print(f"PACKET {role_id} -> {path}")
        made += 1
        if "--all" not in sys.argv:
            break
    for role_id, origin in inflight_ids:
        mark_inflight(role_id, origin)
    print(f"Done: {made} launch packet(s).")


if __name__ == "__main__":
    main()
