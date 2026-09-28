#!/usr/bin/env python3
"""Build a local question inbox from the parked queues.

Cards group matching full questions, show affected leads and bank suggestions,
and separate applicant questions from system blockers. Bare CLI runs are
read-only. ``--deliver`` persists the tray, recurrence history, and watermark
under the shared queue lock with pre-write backups and atomic replacement.
Answers are applied only through tray_answer.py and canonical verification.
"""
import argparse
import copy
import hashlib
import json
import math
import os
import re
import sys
import stat
import tempfile
from contextlib import nullcontext
from pathlib import Path
from datetime import datetime, timedelta, timezone

try:
    from . import tray_sources
    from .keel_paths import HOME, DATA
except ImportError:
    import tray_sources
    from keel_paths import HOME, DATA

BASE = HOME
QDIR = os.path.join(DATA, 'queues')
HDIR = os.path.join(BASE, 'hidden_files')
WM = os.path.join(HDIR, 'input-tray-logged.json')
TRAY_JSON = os.path.join(HDIR, 'input-tray.json')
FAM_HIST = os.path.join(HDIR, 'input-tray-families.json')
BANK = os.path.join(DATA, 'answer_bank.json')

# Integrity-terminal bank keys (2026-09-17, J-20260917-1230-gate-1423):
# standing DO-NOT rules, not answers (the applicant's own words — the
# personally_completed_certification rule is the operator's integrity law: DO NOT
# CERTIFY, revivable only by the operator's personal browser takeover). A draft
# carrying one of these values must never reach an approval path — an
# APPROVE tap on it would certify (or appear to certify) what the law
# forbids. Zero tolerance.
INTEGRITY_TERMINAL_KEYS = frozenset({"personally_completed_certification"})
_INTEGRITY_VALUE_RE = re.compile(r"DO NOT (CERTIFY|ATTEST)", re.I)


def integrity_safe_draft(draft):
    """Strip drafts that carry an integrity-terminal answer.

    Returns None when the draft's bank_key is an integrity-terminal key
    or its value carries an integrity-terminal marker (DO NOT CERTIFY /
    DO NOT ATTEST) — the backstop covers future integrity keys the
    explicit set has not named yet. Any other draft passes through
    unchanged. Pure.
    """
    if not draft:
        return draft
    if (draft.get("bank_key") in INTEGRITY_TERMINAL_KEYS
            or _INTEGRITY_VALUE_RE.search(str(draft.get("value") or ""))):
        return None
    return draft

PROMOTE_THRESHOLD = 3  # family sightings before "promote to profile" is suggested

# Retired/quarantined card keys (2026-09-22 standing directives) — never
# appear in recurring/promote output. These cards are retired by standing
# policy and must not resurface as "standing answer" candidates.
RETIRED_CARD_KEYS = frozenset({
    "d94b6c7b9c8207c0",  # travel — retired 2026-09-22: ≤25% explicit only, park rest
    "21e7331771acdadd",  # interview recording — retired 2026-09-22: no universal consent
    "7a642d5375865492",  # Edmentum/Apex — quarantined 2026-09-22: no valid answer
})


def h(s):
    return hashlib.sha256(s.encode()).hexdigest()[:16]


# Blocker wordings the standing D1 policy already decides -- never tray items.
# 2026-09-22: added 100% onsite — a card asking "Are you able to work 100%
# onsite in <city>?" slipped past the filter (the regex only caught
# "full-time on-site"). D1 parks non-defense full-time on-site without
# re-asking.
POLICY_DECIDED_PAT = re.compile(
    r'4\s*days?/week in office|5\s*days?/week in office|full-?time on-?site|'
    r'100\s*%\s*on-?site',
    re.I)

FAMILY_PAT = re.compile(r'FAMILY\[([^\]/]+)(?:/([^\]]+))?\]')
FAMILY_TAIL_PAT = re.compile(r'\s*\[\d+\s+applications?\]\s*$', re.I)


def _words(s):
    return set(re.findall(r'[a-z]{4,}', s.lower()))


def dedupe(blocks):
    """Collapse near-duplicate blocker wordings within one lead."""
    kept = []
    for k, t in blocks:
        w = _words(t)
        if any(w and _words(e) and len(w & _words(e)) / max(len(w), len(_words(e))) > 0.6
               for _, e in kept):
            continue
        kept.append((k, t))
    return kept


MAX_TRAY_FILE_BYTES = 32 * 1024 * 1024


def _read_bytes(path, limit=MAX_TRAY_FILE_BYTES):
    """Read only bounded regular files, pinning no-follow directory descriptors."""
    path = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    parent = os.open(path.anchor, flags)
    fd = None
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=parent)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("unsafe_or_oversized_tray_source")
        chunks, count = [], 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - count))
            if not chunk:
                break
            chunks.append(chunk)
            count += len(chunk)
            if count > limit:
                raise ValueError("tray_source_byte_limit")
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("tray_source_changed_during_read")
        return b"".join(chunks)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def load(path, default=None):
    """Missing optional state is empty; malformed or unsafe existing state fails closed."""
    try:
        raw = _read_bytes(path)
    except FileNotFoundError:
        return default if default is not None else []
    result = _queue_io().strict_loads(raw)
    if isinstance(default, (list, dict)) and not isinstance(result, type(default)):
        raise ValueError("unexpected_tray_source_shape")
    return result


def genuine_blockers(entry):
    """Yield (key, text) for unresolved items that are genuine the applicant blockers.

    Key scheme is per (lead, blocker text) -- stable across runs so the
    watermark survives the v2 regrouping without re-delivering everything.
    """
    rid = entry.get('role_id', '')
    out = []
    for u in entry.get('unresolved', []) or []:
        t = str(u)
        if t.startswith('RESOLVED'):
            continue
        if POLICY_DECIDED_PAT.search(t):
            continue  # D1 already parks these; not a question for the applicant
        out.append((h(rid + '|' + t), t))
    out = dedupe(out)
    # Merge short fragments back into the previous item: a blocker string
    # sometimes lands in the queue split across list items (e.g. '...needs
    # the applicant's decision' + 'never pre-authorize"'). A short fragment with no
    # question mark and no terminal punctuation is a continuation, not its
    # own question. (A short item containing '?' IS its own question --
    # merging it would let one answer clear two unrelated blockers.)
    merged = []
    for key, text in out:
        s = text.strip()
        if (merged and len(s) < 80 and '?' not in s
                and not s.rstrip().endswith(('.', '!', ':', ';'))):
            _, prev = merged[-1]
            combo = (prev.rstrip() + ' ' + s).strip()
            merged[-1] = (h(rid + '|' + combo), combo)
        else:
            merged.append((key, text))
    return rid, merged


# Digest-layer reclassification (2026-09-17): cards matching these patterns are
# NOT questions the applicant can answer — technical/system states, defect reports, or
# determined ineligibilities. They are reclassified to SYSTEM-BLOCKED at the
# DIGEST LAYER ONLY (write_tray_json below); the underlying queues are never
# mutated to clean the digest. Reclassified cards still appear once in the
# digest's system section (first fresh appearance), then stay quiet — they no
# longer masquerade as NEEDS-YOU questions and bury answerable cards.
#
# Deliberately NOT reclassified (still NEEDS-YOU): no-AI/unaided-work pledges,
# interview-recording consent, essays, personal facts, compensation, D1
# travel/office judgments — those are substantive standing-policy decisions
# where the applicant's ongoing visibility is required, not noise.
PERMANENTLY_BLOCKED_PATTERNS = [
    # Card text already declares itself system-blocked (digest-layer bug:
    # the label was collected but rendered as NEEDS-YOU anyway).
    (r"^SYSTEM-BLOCKED", "card self-declares system-blocked"),
    # Technical states: no answer from the applicant can clear them.
    (r"hcaptcha takeover", "CAPTCHA takeover state, not a question"),
    (r"email.verification code screen|email code screen",
     "email-code gate state, not a question"),
    (r"code.*rejected|approval_unavailable",
     "verification code dead-end, not a question"),
    (r"Browser session approval denied at platform level",
     "platform-level denial, not the applicant's call"),
    (r"React-Select defect|Submit button unresponsive|Submit click blocked",
     "form defect report, not a question"),
    # Answer-consistency guard conflicts: the packet answer diverged from the
    # banked answer (e.g. out_of_scope) and the packet is blocked. The
    # resolution is already banked as a standing rule (DO-NOT-CERTIFY); there
    # is no question the applicant can meaningfully answer Yes/No to. Genuine no-AI /
    # personally-completed pledges do NOT carry this prefix and stay
    # NEEDS-YOU per the design comment above.
    (r"answer-consistency guard",
     "answer-consistency guard-conflict, not a question"),
    # Determined ineligibilities: the honest answer is already fixed and the
    # lead cannot proceed — there is no question to put to the applicant.
    (r"location ineligible|excludes California residents",
     "determined geographic ineligibility"),
    (r"eligible to work for any employer in Canada",
     "determined work-authorization ineligibility"),
    (r"(Luxembourg|EU) work authorization required",
     "determined work-authorization ineligibility"),
    # Routing notes, not questions.
    (r"Application route is apply-by-email",
     "manual apply-by-email route, not a tray question"),
]


def classify_card(question, norm=""):
    """Return the SYSTEM-BLOCKED reason for a card, or None if it is a
    genuine NEEDS-YOU question. Pure function — used by write_tray_json and
    pinned by regression tests."""
    text = f"{question} {norm or ''}"
    for pattern, reason in PERMANENTLY_BLOCKED_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return reason
    return None


def family_of(text):
    m = FAMILY_PAT.search(text)
    if not m:
        return None
    fam = m.group(1).strip().lower().replace(' ', '_')
    var = (m.group(2) or 'general').strip().lower().replace(' ', '_')
    return f"{fam}/{var}"


def normalize_question(text):
    """Canonical question text for cross-lead grouping: strip the FAMILY
    header and the trailing '[N applications]' tail, collapse whitespace."""
    t = FAMILY_PAT.sub('', text)
    t = FAMILY_TAIL_PAT.sub('', t)
    t = re.sub(r'\s+', ' ', t).strip()
    t = re.sub(r'^[:\-–—]\s*', '', t)  # separator left behind by the FAMILY header
    return t


def card_key_for(family, norm):
    return h((family or '') + '|' + norm.lower())


def short_question(norm, limit=160):
    # Prefer the quoted question when the raw text wraps it in quotes
    # ("... (subjective): "What are three adjectives...?") -- the preamble
    # is bookkeeping, the quote is what the applicant must answer.
    m = re.search(r'"([^"]{10,300})"', norm)
    if m:
        q = m.group(1).strip()
        if q.endswith('?') or len(q.split()) >= 4:
            return q[:limit]
    q = norm
    if len(q) > limit:
        q = q[:limit].rsplit(' ', 1)[0] + '...'
    return q


def parse_age_h(stamp):
    """Hours since status_updated; writers use PDT or UTC stamps."""
    if not stamp:
        return None
    s = str(stamp).strip()
    now = datetime.now(timezone.utc)
    dt = None
    for fmt, off in (("%Y-%m-%d %H:%M PDT", -7), ("%Y-%m-%d %H:%M:%S PDT", -7),
                     ("%Y-%m-%d %H:%M UTC", 0), ("%Y-%m-%d %H:%M:%S UTC", 0),
                     ("%Y-%m-%dT%H:%M:%S", 0)):
        try:
            naive = datetime.strptime(s, fmt)
            dt = (naive - timedelta(hours=off)).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
    if dt is None:
        return None
    return max(0.0, (now - dt).total_seconds() / 3600.0)


def load_bank():
    data = load(BANK, {})
    answers = data.get('answers', {}) if isinstance(data, dict) else {}
    banked = []
    for k, v in answers.items():
        if k.startswith('_'):
            continue
        if isinstance(v, dict):
            if v.get('draftable') is False:
                continue  # principle/policy reference entries never surface as drafts
            # structured bank entry (patterns + scope): the human-readable
            # answer is what a draft should show, not the JSON blob.
            # Entries store it under 'value' (older ones under 'answer').
            val = str(v.get('value', v.get('answer', '')))
            scope = str(v.get('scope', ''))
        else:
            val = v if isinstance(v, str) else json.dumps(v)[:200]
            scope = ''
        banked.append((k, val, scope))
    return banked


# Tail tokens that are generic answer classes, not employer names -- a bank
# key ending in one of these is employer-agnostic and never vetoed.
_GENERIC_TAIL = {'general', 'willingness', 'policy', 'consent', 'question',
                 'answer', 'default', 'standard', 'attestation',
                 'certification', 'form', 'required', 'profile'}


def _key_employer(key):
    tail = key.lower().split('_')[-1]
    if tail in _GENERIC_TAIL or len(tail) < 4:
        return None
    return tail


def draft_for(norm, banked, employer=None):
    """Draft-first: fuzzy-match the question against banked answers.

    Conservative: suggest only on strong token overlap with the bank key
    (bank keys are snake_case topic names). Never auto-applies.

    Employer veto: a bank key written for one employer (trailing token,
    e.g. prior_employment_exampleco) is suggested ONLY on questions that
    actually name that employer. A mismatched draft could get approved
    into the wrong attestation -- silence beats a wrong suggestion.
    Scope veto (2026-09-22): a bank entry with scope "employer:X" is
    suggested ONLY when the question or the lead's employer names X.
    A per-employer consent/approval is never reused outside its original
    scope (2026-09-22: interview-recording approvals stay
    employer/provider-specific).
    """
    qw = _words(norm)
    norm_low = norm.lower()
    emp_low = (employer or '').lower()
    best = None
    for key, val, scope in banked:
        kw = {w for w in key.lower().split('_') if len(w) >= 3}
        if not kw or not qw:
            continue
        inter = qw & kw
        score = len(inter) / len(kw)
        if score >= 0.6 and len(inter) >= 2:
            ke = _key_employer(key)
            if ke and ke not in norm_low:
                continue  # banked for an employer this question doesn't name
            if scope.startswith('employer:'):
                scoped = scope.split(':', 1)[1].strip().lower()
                if scoped and scoped not in norm_low and scoped not in emp_low:
                    continue  # scoped approval; this question/employer is out of scope
            if best is None or score > best[0]:
                best = (score, key, val)
    if best:
        val = best[2]
        if isinstance(val, dict):  # structured bank entry passed raw
            val = str(val.get('value', val.get('answer', '')))
        return {'bank_key': best[1], 'value': str(val)[:160]}
    return None


def fmt_age(hours):
    if hours is None:
        return "age unknown"
    if hours < 1:
        return "<1h"
    if hours < 48:
        return f"{int(hours)}h"
    return f"{int(hours // 24)}d"


def _min_fit(value=None):
    value = os.environ.get("KEEL_TRAY_MIN_FIT", "60") if value is None else value
    if isinstance(value, bool):
        raise ValueError("minimum fit must be a finite number from 0 to 100")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("minimum fit must be a finite number from 0 to 100") from exc
    if not math.isfinite(number) or not 0 <= number <= 100:
        raise ValueError("minimum fit must be a finite number from 0 to 100")
    return number


def collect_cards(min_fit=None):
    """Scan queues into cards; KEEL_TRAY_MIN_FIT defaults to the current bar, 60."""
    min_fit = _min_fit(min_fit)
    banked = load_bank()
    cards = {}  # card_key -> card dict
    for fname in ('needs_input-queue.json', 'standard-queue.json'):
        for e in load(os.path.join(QDIR, fname), []):
            if 'NEEDS-INPUT' not in str(e.get('status', '')):
                continue
            try:
                fit = float(e.get('fit_score') or 0)
            except (TypeError, ValueError, OverflowError):
                fit = 0
            if not math.isfinite(fit) or not 0 <= fit <= 100 or fit < min_fit:
                continue
            rid, blocks = genuine_blockers(e)
            age = parse_age_h(e.get('status_updated'))
            employer = e.get('employer') or e.get('company') or ''
            title = e.get('title') or ''
            for key, text in blocks:
                fam = family_of(text)
                norm = normalize_question(text)
                ck = card_key_for(fam, norm)
                card = cards.get(ck)
                if card is None:
                    card = cards[ck] = {
                        'key': ck, 'family': fam,
                        'question': short_question(norm),
                        'norm': norm,
                        'status': 'NEEDS-YOU',
                        'leads': [], 'item_keys': [],
                        'draft': None,
                    }
                card['leads'].append({
                    'role_id': rid, 'employer': employer, 'title': title,
                    'fit': e.get('fit_score'), 'parked_h': age,
                })
                card['item_keys'].append(key)
    for card in cards.values():
        fits = [float(l['fit'] or 0) for l in card['leads']]
        card['unblock_leads'] = len(card['leads'])
        card['unblock_fit'] = round(sum(fits), 1)
        ages = [l['parked_h'] for l in card['leads'] if l['parked_h'] is not None]
        card['oldest_parked_h'] = round(max(ages), 1) if ages else None
        # Multi-employer guard (2026-09-22): a card spanning more than one
        # employer must never carry an employer-scoped draft -- approving it
        # would apply one employer's consent to another's application.
        # Passing employer=None vetoes every employer-scoped draft; only
        # global drafts survive. (Approvals are reused only within their
        # original scope.)
        _emps = {str(l.get('employer') or '').strip().lower() for l in card['leads']}
        _emps.discard('')
        _one_emp = card['leads'][0]['employer'] if len(_emps) == 1 and card['leads'] else None
        card['draft'] = draft_for(card['norm'], banked, employer=_one_emp)
        card['leads'].sort(key=lambda l: float(l['fit'] or 0), reverse=True)
    return cards


def _queue_io():
    try:
        from . import queue_io
    except ImportError:
        import queue_io
    return queue_io


def _persist_json(path, payload):
    """Back up the exact previous bytes before a lock-protected atomic write."""
    io = _queue_io()
    with io.queue_lock(owner="input_tray_digest:persist"):
        os.makedirs(HDIR, exist_ok=True)
        try:
            previous = _read_bytes(path)
        except FileNotFoundError:
            previous = None
        if previous is not None:
            fd, backup = tempfile.mkstemp(
                prefix="_backup-" + os.path.basename(path) + "-", dir=HDIR)
            with os.fdopen(fd, "wb") as saved:
                saved.write(previous)
                saved.flush()
                os.fsync(saved.fileno())
            io._dir_fsync(HDIR)
        io.atomic_write_json(path, payload)


def update_family_history(cards, fresh_keys, now_iso, delivered, *, persist=True):
    """Recurrence history. times_seen counts DELIVERIES only -- a dry run
    must never inflate it (that once fabricated 84 'recurring' candidates)."""
    hist = load(FAM_HIST, {})
    if not isinstance(hist, dict):
        hist = {}
    for ck, card in cards.items():
        rec = hist.get(ck, {})
        rec['family'] = card['family']
        rec['question'] = card['question']
        rec['first_seen'] = rec.get('first_seen', now_iso)
        rec['last_seen'] = now_iso
        if delivered and any(k in fresh_keys for k in card['item_keys']):
            rec['times_seen'] = rec.get('times_seen', 0) + 1
        rec['last_unblock_leads'] = card['unblock_leads']
        hist[ck] = rec
    if persist:
        _persist_json(FAM_HIST, hist)
    return hist


def _decorate_cards(cards):
    """Refresh evidence-bound QRESOLVE drafts without making their service mandatory."""
    original = copy.deepcopy(cards)
    try:
        if __package__:
            from .qresolve import decorate_cards
        else:
            from qresolve import decorate_cards
        decorated = decorate_cards(copy.deepcopy(cards))
        if (not isinstance(decorated, list)
                or len(decorated) != len(cards)
                or any(not isinstance(card, dict) for card in decorated)
                or [card.get("key") for card in decorated] != [card["key"] for card in cards]):
            raise ValueError("invalid proposal decoration")
        cards = decorated
    except Exception:
        # An unavailable resolver must not leave an apparently valid resolver-owned
        # answer behind. Existing human/bank suggestions are outside its ownership.
        cards = original
        for card in cards:
            draft = card.get("draft")
            if isinstance(draft, dict) and draft.get("owner") == "qresolve":
                card["draft"] = None
    for card, before in zip(cards, original):
        if before.get("status") == "SYSTEM-BLOCKED":
            card["status"] = "SYSTEM-BLOCKED"
            card["draft"] = None
    return cards


def write_tray_json(cards, fresh_keys, hist, system_blocked, now_iso, *, persist=True):
    payload_cards = []
    for ck, card in cards.items():
        if ck in RETIRED_CARD_KEYS:
            continue  # retired by standing policy (2026-09-22) —
            # never surfaces in ANY digest section (main cards, promote,
            # or recurring). The 2026-09-23 hotfix only covered promote.
        fresh_n = sum(1 for k in card['item_keys'] if k in fresh_keys)
        rec = hist.get(ck, {})
        # Digest-layer reclassification (2026-09-17): cards no answer can
        # clear become SYSTEM-BLOCKED here. Queues are never mutated.
        # Integrity law (2026-09-17, J-20260917-1230-gate-1423): a
        # SYSTEM-BLOCKED card carries no approvable answer by definition —
        # never attach a draft carrying an integrity-terminal answer
        # (DO NOT CERTIFY). Zero tolerance on such a value surfacing in
        # any approval path.
        # Keel 0.4 P3 (2026-09-17): blocker-source taxonomy (tray_sources).
        # intel_gap / software_error / stale_packet are robot problems, never
        # questions for the applicant: they take the SYSTEM-BLOCKED surface with a
        # taxonomy reason and never render as NEEDS-YOU. The existing
        # PERMANENTLY_BLOCKED_PATTERNS reclassification (including the
        # load-bearing "Deliberately NOT reclassified" carve-out for
        # integrity classes) is untouched; queues are never mutated.
        # Every card carries "source" in the 4-value set (acceptance §5).
        blocked_reason = classify_card(card['question'], card.get('norm', ''))
        source = tray_sources.classify_source(card['question'],
                                              card.get('norm', ''))
        if source in ("intel_gap", "software_error", "stale_packet"):
            tax_reason = tray_sources.SYSTEM_REASONS[source]
            blocked_reason = (tax_reason if not blocked_reason
                              else f"{tax_reason}; also: {blocked_reason}")
        draft = None if blocked_reason else integrity_safe_draft(card['draft'])
        payload_cards.append({
            'key': ck,
            'family': card['family'],
            'question': card['question'],
            'norm': card.get('norm', card['question']),
            'status': 'SYSTEM-BLOCKED' if blocked_reason else 'NEEDS-YOU',
            'blocked_reason': blocked_reason,
            'source': source,
            'unblock_leads': card['unblock_leads'],
            'unblock_fit': card['unblock_fit'],
            'oldest_parked_h': card['oldest_parked_h'],
            'draft': draft,
            'fresh': fresh_n > 0,
            'fresh_items': fresh_n,
            'times_seen': rec.get('times_seen', 0),
            'first_seen': rec.get('first_seen'),
            'leads': [
                {'role_id': l['role_id'], 'employer': l['employer'],
                 'title': l['title'], 'fit': l['fit'],
                 'parked_h': l['parked_h']}
                for l in card['leads']
            ],
        })
    payload_cards = _decorate_cards(payload_cards)
    # unblock-value ordering: most leverage first
    payload_cards.sort(key=lambda c: (-c['unblock_fit'], -(c['oldest_parked_h'] or 0)))
    answerable_keys = {c['key'] for c in payload_cards if c['status'] == 'NEEDS-YOU'}
    promote = [
        {'key': ck, 'family': r.get('family'), 'question': r.get('question'),
         'times_seen': r.get('times_seen', 0)}
        for ck, r in hist.items()
        if r.get('times_seen', 0) >= PROMOTE_THRESHOLD and ck in cards
        and ck not in RETIRED_CARD_KEYS
        and ck in answerable_keys
    ]
    promote.sort(key=lambda p: -p['times_seen'])
    payload = {
        'generated_at': now_iso,
        'cards': payload_cards,
        'system_blocked': system_blocked,
        'promote_candidates': promote,
        'note': ("NEEDS-YOU cards need the applicant's words; SYSTEM-BLOCKED counts "
                 "are robot problems on a separate surface. Answering one card "
                 "clears every lead on it (tray_answer.py)."),
    }
    if persist:
        _persist_json(TRAY_JSON, payload)
    return payload


def system_blocked_counts():
    """Separate taxonomy: robot problems are counted, never tray questions."""
    captcha = load(os.path.join(QDIR, 'captcha-queue.json'), [])
    return {
        'captcha_parked': len(captcha) if isinstance(captcha, list) else 0,
        'note': 'system health surface, not questions for the applicant',
    }


def _triage_line(c):
    """One-tap triage brief for a card. Fail-safe: never breaks the digest."""
    try:
        from tray_triage import brief_line
        return brief_line(c)
    except Exception:
        return ""


def render_digest(payload):
    cards = payload['cards']
    # Digest-layer reclassification: SYSTEM-BLOCKED cards are rendered on
    # their own surface with reasons — never as numbered NEEDS-YOU questions.
    need_you = [c for c in cards if c.get('status') != 'SYSTEM-BLOCKED']
    sys_blocked = [c for c in cards if c.get('status') == 'SYSTEM-BLOCKED']
    lines = []
    lines.append("INPUT TRAY — new items needing the applicant's answers "
                 "(pipeline keeps running; nothing here blocks other leads):")
    lines.append("")
    for i, c in enumerate(need_you, 1):
        fam = f"[{c['family']}] " if c['family'] else ""
        lines.append(f"{i}. {fam}{c['question']}")
        lines.append(
            f"   unlocks {c['unblock_leads']} lead{'s' if c['unblock_leads'] != 1 else ''} "
            f"(fit {c['unblock_fit']}) · waiting {fmt_age(c['oldest_parked_h'])} "
            f"· key {c['key']}")
        if c['draft']:
            lines.append(
                f"   draft from your bank [{c['draft']['bank_key']}]: "
                f"\"{c['draft']['value']}\" — reply APPROVE, edit it, or answer fresh")
        shown = c['leads'][:3]
        lead_str = "; ".join(
            f"{l['employer']} — {l['title']} (fit {l['fit']})" for l in shown)
        more = f" +{c['unblock_leads'] - 3} more" if c['unblock_leads'] > 3 else ""
        lines.append(f"   covers: {lead_str}{more}")
        lines.append(_triage_line(c))
        lines.append("")
    if payload['promote_candidates']:
        lines.append("Recurring questions (seen 3+ times) — consider a standing answer "
                     "so the tray stays quiet:")
        for p in payload['promote_candidates'][:3]:
            fam = f"[{p['family']}] " if p['family'] else ""
            lines.append(f"  · {fam}{p['question'][:100]} (seen {p['times_seen']}x, key {p['key']})")
        lines.append("")
    sb = payload['system_blocked']
    if sb['captcha_parked']:
        lines.append("System-blocked (not questions for you — robot problems, separate surface): "
                     f"{sb['captcha_parked']} CAPTCHA-parked.")
        lines.append("")
    if sys_blocked:
        # Keel 0.4 P3: taxonomy-blocked cards (intel_gap / software_error /
        # stale_packet) render once on their own system surface with the
        # spec §3.4 line — never as numbered NEEDS-YOU questions. The legacy
        # permanently-blocked section below is unchanged.
        taxonomy = [c for c in sys_blocked
                    if c.get('source') in ("intel_gap", "software_error",
                                           "stale_packet")]
        legacy = [c for c in sys_blocked
                  if c.get('source') not in ("intel_gap", "software_error",
                                             "stale_packet")]
        if taxonomy:
            lines.append("System-blocked (not questions — robot problems with "
                         "a named owner and retry path; leads stay parked):")
            for c in taxonomy:
                line = tray_sources.system_line(c)
                lines.append(f"  · {line or c.get('blocked_reason') or 'system-blocked'}")
            lines.append("")
        if legacy:
            lines.append("Permanently blocked (not questions — nothing you say can clear "
                         "these; kept visible for the record, leads stay parked):")
            for c in legacy:
                fam = f"[{c['family']}] " if c['family'] else ""
                reason = c.get('blocked_reason') or 'system-blocked'
                lines.append(f"  · {fam}{c['question'][:110]} — {reason} "
                             f"(key {c['key']}, {c['unblock_leads']} lead(s))")
            lines.append("")
    lines.append("Reply in this chat with answers (cite the key); each gets banked and "
                 "clears every listed lead through canonical verify.")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Input-tray digest.")
    ap.add_argument('--deliver', action='store_true',
                    help='Persist the tray, history, and delivery watermark. '
                         'Without this flag no workspace files are changed.')
    ap.add_argument('--min-fit', type=float, default=None,
                    help='Minimum fit from 0 to 100 (default KEEL_TRAY_MIN_FIT or 60).')
    args = ap.parse_args(argv)
    try:
        minimum = _min_fit(args.min_fit)
    except ValueError as exc:
        ap.error(str(exc))

    lock = (_queue_io().queue_lock(owner="input_tray_digest:deliver")
            if args.deliver else nullcontext())
    with lock:
        now_iso = datetime.now(timezone.utc).isoformat()
        cards = collect_cards(min_fit=minimum)
        wm = load(WM, [])
        seen = set(wm if isinstance(wm, list) else [])
        fresh_keys = {k for card in cards.values() for k in card['item_keys']
                      if k not in seen}
        hist = update_family_history(cards, fresh_keys, now_iso, args.deliver,
                                     persist=args.deliver)
        payload = write_tray_json(cards, fresh_keys, hist,
                                  system_blocked_counts(), now_iso,
                                  persist=args.deliver)
        fresh_payload = dict(payload)
        fresh_payload['cards'] = [c for c in payload['cards'] if c['fresh']]
        if not fresh_payload['cards']:
            print('TRAY-QUIET')
            return 0
        print(render_digest(fresh_payload))
        if args.deliver:
            seen.update(fresh_keys)
            _persist_json(WM, sorted(seen))
    return 0


if __name__ == '__main__':
    sys.exit(main())
