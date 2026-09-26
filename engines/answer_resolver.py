#!/usr/bin/env python3
"""Context-aware answer resolver (F34 fix, P0).

bank_scope.py governs WRITES (which scope a new entry gets). This module
governs READS: it is the SINGLE context-aware read path every consumer of
answer_bank.json must go through. Where the old code flattened every entry
through bank_value() — dropping scope metadata before use — this resolver
returns the value PLUS its authority metadata (scope, source/provenance,
expiry), or ABSTAINS when the banked answer's scope does not cover the
requesting employer/role.

Contract:
    res = resolve(key, entry, employer=None, role_context=None)

    res.status == "resolved"  -> res.value is authorized for THIS
                                 employer/role; res.scope / res.source /
                                 res.expiry carry the authority metadata.
    res.status == "abstain"   -> no banked answer is authorized for this
                                 employer/role. Consumers MUST leave the
                                 field blank / park, never fill.
    res.status == "legacy"    -> scope unestablished (pre-scope entry);
                                 the value is present but FLAGGED. Consumers
                                 must treat flagged-legacy as
                                 abstain-by-default for attestations/consents;
                                 other consumers may render it only with an
                                 explicit [LEGACY] marker, never as a
                                 silently global fact.

Rules (first match wins):
  1. Missing/empty entry          -> abstain (never invent).
  1b. Banked REFUSAL value ("DO NOT CERTIFY ... route to PERSONAL
      TAKEOVER" and kin)          -> abstain: a refusal directive is not an
                                     answer (ARM1 adjudication 2026-09-18).
                                     No consumer may render, fill, or
                                     compare it as one.
  2. Machine scope "ambiguous"    -> abstain (quarantined).
  3. Machine scope "global"       -> resolve (unless expired).
  4. Machine scope "employer:X"   -> resolve iff requester employer matches
                                     X; else abstain.
  5. Free-text scope/employer_scope (legacy entries) is PARSED:
       "role_id <ID> only"        -> resolve iff role_context names <ID>.
       "<Name> ONLY"              -> resolve iff employer matches <Name>.
       employer names listed       -> resolve iff employer is one of them.
       "ALL leads"                 -> resolve, flagged legacy.
       no parseable scoping        -> flagged legacy, explicit.
  6. Expired authority (expires/expiry/valid_until in the past) -> abstain.
  7. Sensitive key (consent/attestation/no-AI/recording/...) with flagged
     legacy -> abstain-by-default, UNLESS the key is one of the applicant's
     EXPLICITLY pre-authorized standing attestations (named allowlist,
     sourced from answer_bank.json autopilot_attestation_scope + standing
     AI-evaluation consent). Even then the resolution is tagged with that
     exact standing authority — never silently global.

Fail-closed: any exception during resolution -> ABSTAIN.
Never invents: the resolver returns banked values or abstains — nothing else.

Identity type validation (validate_identity_field): email/phone/name-shaped
values are type-checked before POST assembly. A value of the wrong type
fails closed to abstain (identity_type) rather than landing in a payload.

Concurrency note (red-team 2026-09-17): the employer and role_context
inputs are snapshotted to plain strings at resolve() entry — a caller
mutating its role dict mid-call cannot change a resolution in flight.
Callers are single-threaded and pass per-call role snapshots (never shared
mutable state); the resolver performs no I/O and holds no locks, so there
is no time-of-check/time-of-use window beyond the call itself.

known_employers() (from bank_scope) is consulted ONCE per resolve(), and
only to detect employer names inside LEGACY free-text scope fields. A
registry that is cleared or changed between calls can only move a legacy
entry toward legacy/abstain — never toward an unauthorized resolve:
machine scopes ("global", "employer:<name>", "ambiguous") never consult
the registry, and an undetectable employer name falls to the flagged
legacy-unknown branch above.

Compound-growth snapshot (2026-09-21): resolve() accepts an optional
``registry`` snapshot (a pre-loaded set of employer names). When given,
the snapshot is used INSTEAD of calling known_employers() — one registry
read serves a whole reuse session instead of one disk read per legacy
resolution. The snapshot is still a name-detection aid, never authority:
staleness only degrades toward abstain (fewer names detected), never
toward cross-employer leakage. answer_reuse.ReuseSession owns this
pattern; bare resolve() calls keep the per-call behavior.

Registry-poisoning note (red-team 2026-09-17, structurally closed round
9): the registry is a NAME DETECTION aid, never an authority. An
("employers-exact", names) parse only authorizes when the REQUESTING
employer itself exactly matches one of the names in the scope text
(_norm_eq: no prefix-guessing) AND an independent canonical employer
binding from role_context["company"|"employer"] (the queue lead being
processed) corroborates the requester. Registry + scope text + requester
agreeing is self-referential — a poisoned registry naming AttackerCorp
cannot make an "AttackerCorp-only" text authorize for an AttackerCorp
requester without that independent binding (proven by
test_attackercorp_registry_self_reference_abstains). Sensitive keys
additionally abstain-by-default on any legacy parse; unlisted keys
abstain via the safe-legacy allowlist.

Trust boundary (red-team 2026-09-17, round 9): known_employers() reads
the applicant's LOCAL queue JSON files; the answer bank is the applicant's local file.
There is no network registry, no shared config, no ingestion pipeline —
a "registry attacker" with local file write already owns answer_bank.json
outright (where "scope": "global" bypasses everything), so the registry
is not a privilege boundary. The invariant the resolver guarantees does
not depend on registry integrity: the bank entry's own scope text is
authoritative, a legacy text can only ever authorize the employer(s) it
names for a matching requester, and a legacy free-text employer scope
additionally requires the independent canonical employer binding from
the role context. Registry corruption (cleared, stale, or
over-inclusive) therefore degrades to AVAILABILITY (false abstains,
proven by test) — never to cross-employer LEAKAGE.

Double-scope rule (red-team 2026-09-17): an entry carrying BOTH a machine
"scope" and a legacy "employer_scope" is governed by the machine scope
alone; the legacy text is never parsed, never authorizes, and never
leaks into the Resolution (proven by
test_machine_scope_wins_over_contradictory_legacy). employer_scope is
read only by this module — no other consumer touches it.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone

# K50 (2026-09-18, Keel 0.3.1 review): strict JSON parse for the bank.
# queue_io is lock-free at import (no lock taken on import); its
# strict_loads is a pure parser, so this adds no lock semantics to the
# resolver's descriptor-hardened load path.
import queue_io

# bank_scope is the governed-memory module this resolver sits on top of.
from bank_scope import (bank_scope as _machine_scope,
                        bank_value as _bank_value)

try:
    from bank_scope import known_employers as _known_employers
except Exception:  # pragma: no cover — fail-safe
    def _known_employers():  # type: ignore
        return set()

# ------------------------------------------------------------------ bank load

#: Maximum answer-bank file size parsed in one go (2 MiB). The bank is
#: the applicant-local (not untrusted input), but a corrupted or runaway file must
#: fail closed with a clear error rather than exhausting memory in
#: json.load before the resolver ever sees a key (red-team 2026-09-17).
BANK_MAX_BYTES = 2 * 1024 * 1024


def load_bank(path):
    """Load and parse an answer-bank JSON file with a size cap.

    Raises ValueError when the file is missing, oversized, or unparsable —
    callers treat this as "no banked answers" (fail-closed), never as an
    excuse to invent values.

    Descriptor-hardened (red-team 2026-09-17, round 9):
    - os.open with O_NOFOLLOW: symlinks are refused outright (a symlink
      swap between check and read cannot redirect us to another file).
    - O_NONBLOCK: opening a fifo never blocks waiting for a writer.
    - stat.S_ISREG on the open descriptor: directories, fifos, sockets,
      and device nodes are never parsed as banks.
    - Size via fstat on the OPEN descriptor: no getsize/open TOCTOU — even
      a swapped path cannot make us read more than the cap from the
      descriptor we actually parse.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ValueError(f"answer bank unreadable: {exc}")
    try:
        try:
            st = os.fstat(fd)
        except OSError as exc:
            raise ValueError(f"answer bank unreadable: {exc}")
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(
                "answer bank is not a regular file — refusing to parse "
                "(symlinks, directories, fifos, sockets, and device nodes "
                "are never bank sources)")
        if st.st_size > BANK_MAX_BYTES:
            raise ValueError(
                f"answer bank too large ({st.st_size} bytes > "
                f"{BANK_MAX_BYTES}) — refusing to parse")
        fh = os.fdopen(fd, "r", encoding="utf-8")
        fd = None  # ownership transferred to fh; the with-block closes it
        with fh:
            try:
                # K50 (2026-09-18): strict parse — duplicate keys,
                # non-finite numbers, and float overflow fail closed here
                # with ValueError, wrapped into the standard unparsable
                # message below. The bank file is the applicant-local but a
                # partially-written corrupt file must never parse to a
                # last-wins merge.
                data = queue_io.strict_loads(fh.read())
            except ValueError as exc:
                raise ValueError(f"answer bank unparsable: {exc}")
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    # Logical sanity (red-team 2026-09-17): a partial write / corrupt file
    # that parses to the wrong shape fails closed here, not downstream.
    if not isinstance(data, dict):
        raise ValueError("answer bank corrupt: top-level JSON is not an object")
    return data


# ------------------------------------------------------------------ status

STATUS_RESOLVED = "resolved"
STATUS_ABSTAIN = "abstain"
STATUS_LEGACY = "legacy"

SCOPE_GLOBAL = "global"
SCOPE_LEGACY_UNKNOWN = "legacy-unknown"


#: Residence keys — the applicant's current location facts. These resolve ONLY from
#: a the applicant-authorized bank entry: machine scope "global" whose provenance
#: records the applicant's own words. Anything less (legacy/ambiguous scope,
#: employer-scoped, inferred value, missing provenance) ABSTAINS everywhere
#: the resolver is used (brief render, consistency guard, draft fill,
#: launch vetting) — the value is never rendered, filled, or compared.
_RESIDENCE_KEYS = frozenset({"current_zip", "current_city"})


def is_operator_authorized_residence(key, entry) -> bool:
    """True when `entry` for a residence key is operator-authorized.

    Requires: dict entry, machine scope "global", and provenance/source
    text recording the applicant's own words. Fail-closed on any read error
    or missing field. The authorization is the applicant's words + global
    scope — the VALUE is whatever they stated (never pinned to one ZIP/city
    here, so a future move they state themself keeps working). When
    KEEL_OPERATOR_NAME is set, the provenance must also name the operator;
    otherwise "own words" provenance suffices.
    """
    try:
        if str(key or "") not in _RESIDENCE_KEYS:
            return False
        if not isinstance(entry, dict):
            return False
        if _machine_scope(entry) != SCOPE_GLOBAL:
            return False
        prov = _source(entry).lower()
        if "own words" not in prov:
            return False
        op = os.environ.get("KEEL_OPERATOR_NAME", "").strip().lower()
        if op:
            return op in prov
        return True
    except Exception:
        return False


@dataclass
class Resolution:
    """The outcome of resolving one bank key for one employer/role."""
    key: str
    status: str                      # resolved | abstain | legacy
    value: str = ""
    scope: str = SCOPE_LEGACY_UNKNOWN
    source: str = ""                 # provenance / authority text
    expiry: str = ""                 # ISO string, "" when none recorded
    legacy: bool = False             # True when scope was not established
    reason: str = ""                 # why abstained / why flagged


# ---------------------------------------------------------------- employers

_LEGAL_SUFFIX_RE = re.compile(
    r"\b(llc|inc|incorporated|co|corp|corporation|company|ltd|limited|"
    r"labs|laboratories|technologies|technology|group|holdings|holding|"
    r"plc|gmbh|pbc|pa|llp|lp|dba)\b", re.I)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_employer(name) -> str:
    """Canonical employer token for scope comparison (fail-safe)."""
    try:
        s = str(name or "").strip().lower()
        s = _LEGAL_SUFFIX_RE.sub(" ", s)
        s = _NON_ALNUM_RE.sub(" ", s)
        return re.sub(r"\s+", " ", s).strip()
    except Exception:
        return ""


def employers_match(a, b) -> bool:
    """Conservative employer-name equality (fail-closed on ambiguity).

    Matches on normalized-token equality or a strict word-boundary prefix
    (so "Samsara Inc." matches "Samsara" but "Code" never matches
    "CodePath").
    """
    try:
        x, y = normalize_employer(a), normalize_employer(b)
        if not x or not y or len(x) < 2 or len(y) < 2:
            return False
        if x == y:
            return True
        return x.startswith(y + " ") or y.startswith(x + " ")
    except Exception:
        return False


# ------------------------------------------------------- sensitivity classes

#: Key-name tokens that mark consent/attestation judgments. A legacy entry
#: under one of these keys can never be treated as a standing global fact
#: without the applicant's explicit standing pre-authorization.
_SENSITIVE_KEY_RE = re.compile(
    r"(consent|attest|no_ai|unaided|recording|sms|whatsapp|text_message|"
    r"texting|arbitration|background_check|background-check|at_will|"
    r"biometric|fingerprint|retina|iris_scan|drug_test|medical|hipaa|"
    r"polygraph|credit_check|waiver|authorization|authorize|acknowledg|"
    r"certif|signature|release|disclosure)")

#: MAINTENANCE OBLIGATION (red-team 2026-09-17): this regex is a heuristic
#: layer — when a genuinely new consent/attestation/commitment key shape is
#: banked (e.g. via tray_answer), add its stem here. The fail-safe
#: direction is documented: an unrecognized future key resolves as
#: flagged legacy (visible in briefs, never silent), and the sensitive
#: classes that matter most (consent/attest/authorize/waiver/acknowledg)
#: are covered by stem, not by exact key name.

#: the applicant's EXPLICIT standing pre-authorization (answer_bank.json key
#: "autopilot_attestation_scope", Full autopilot 2026-09-16, plus the
#: standing AI-evaluation consent). ONLY these keys may resolve out of a
#: flagged-legacy sensitive entry — and only with the standing authority
#: named on the resolution. Everything else sensitive + legacy abstains.
PREAUTHORIZED_ATTESTATION_KEYS = frozenset({
    "arbitration_agreement",
    "background_check_consent",
    "at_will_acknowledgment",
    "information_truthfulness_attestation",
    "data_privacy_consent",
    "ai_evaluation_consent",
})
_PREAUTH_SOURCE = (
    "operator standing pre-authorization (answer_bank.json "
    "autopilot_attestation_scope plus banked AI-evaluation consent)")

#: Explicit safe-legacy allowlist (red-team round 9 — structural
#: default-deny). A legacy-scope entry (no machine scope, no parseable
#: free-text scope) resolves as flagged legacy ONLY when its key is on
#: this list or in PREAUTHORIZED_ATTESTATION_KEYS. Any other legacy key —
#: including future consent/attestation-shaped keys the _SENSITIVE_KEY_RE
#: heuristic has never seen — ABSTAINS (sensitivity-unknown, never
#: silently treated as a safe standing fact). MAINTENANCE OBLIGATION: new
#: keys banked via tray_answer MUST be classified here (or preauthorized)
#: before they can resolve from legacy scope; the resolver will not guess.
_SAFE_LEGACY_KEYS = frozenset({
    "addepar_competitor_employment",
    "age_18_or_older",
    "ai_agents_built_deployed",
    "ai_industry_years",
    "ai_tools_used",
    "bachelors_degree",
    "chownow_salesforce_gong_outreach",
    "clearance",
    "current_company_name",
    "current_employer",
    "current_job_title",
    "current_location",
    "current_role_end_date",
    "currently_employed_abnormal_security",
    "currently_employed_maven_clinic",
    "data_infra_devtooling_experience",
    "degree_or_experience_equivalency",
    "deloitte_employment_history",
    "education",
    "eeo_optional",
    "email",
    "employer_acquaintance_default_no",
    "essay_codepath_operational_change",
    "essay_codepath_why_interested",
    "essay_goodfire_why_bom",
    "essay_goodfire_why_cos",
    "essay_remote_cx_tooling_cs_partnership",
    "finra_licenses_held",
    "first_name",
    "heard_about",
    "heard_about_no_other_option_rule",
    "last_name",
    "legal_name_if_different",
    "linkedin",
    "llm_orchestration_agentic_frameworks",
    "location",
    "needs_sponsorship",
    "no_digital_marketing_tenure",
    "no_product_feed_management",
    "noncompete",
    "office_frequency_park_decision",
    "office_location_policy",
    "phone",
    "phone_country",
    "portfolio_url",
    "postal_code",
    "preferred_name",
    "rag_systems",
    "relocation_willingness",
    "remote_work_experience",
    "remote_work_intent_yesno",
    "scripting_problem_solving",
    "sql_proficiency",
    "start_timeframe",
    "stream_implementation_onboarding_enablement",
    "street_address",
    "sw_eng_10yr_no",
    "tech_lead_3yr_no",
    "third_party_references_do_not_invent",
    "total_professional_years",
    "us_citizen_resident",
    "us_work_auth",
    "work_intent",
    "work_location_intent",
    "work_state",
    "zip_code",
})


def _is_sensitive_key(key: str) -> bool:
    try:
        return bool(_SENSITIVE_KEY_RE.search(str(key or "").lower()))
    except Exception:
        return True  # fail-safe: unknown -> sensitive direction


# ----------------------------------------------------------- legacy parsing

_ONLY_RE = re.compile(
    r"\b([A-Z][A-Za-z&'.-]{1,}(?:\s+[A-Z][A-Za-z&'.-]{1,}){0,3})\s+ONLY\b")
_ROLEID_RE = re.compile(r"role_id\s+([A-Za-z0-9_.-]+)\s+only", re.I)
_ALL_LEADS_RE = re.compile(r"\bALL\s+leads\b", re.I)
_CAP_PHRASE_RE = re.compile(
    r"\b([A-Z][A-Za-z&'.-]{1,}(?:\s+[A-Z][A-Za-z&'.-]{1,}){0,3})\b")
#: Leading prepositions/articles the phrase extractor folds into a name
#: ("For Circle Medical") — stripped for a second match attempt.
_STOP_LEAD_RE = re.compile(r"^(for|at|with|from|to|of|the|a|an)\s+", re.I)
#: Boilerplate words that must never scope an entry on their own.
_STOP_WORDS_RE = re.compile(
    r"^(for|at|with|from|to|of|the|a|an|and|or|in|on|all|only|"
    r"application|applications|applies|apply|per|role|roles)$", re.I)
#: Raw alphanumeric tokens (catches lowercase/digit registry names the
#: capitalized-phrase extractor misses).
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9&'.-]*")


_PAREN_RE = re.compile(r"\([^)]*\)")


def _norm_eq(a, b) -> bool:
    """Exact normalized employer-name equality (no prefix rule).

    Used for LEGACY free-text attribution (red-team 2026-09-17): when the
    registry holds overlapping names ("Apple", "Apple Pie Inc."), a text
    naming "Apple" must attribute to Apple only — never guess the longer
    name via prefix. Legal suffixes/punctuation/case still normalize away,
    so "Samsara (Holdings) LLC" == "Samsara". Parenthetical qualifiers are
    ignored as a fallback ("Bartholomew Estate Winery (Bartholomew
    Foundation)" == "Bartholomew Estate Winery") — verified safe on the
    real registry: the only paren-collision pair is the same employer
    twice (LodgeWorks property annotations).
    """
    try:
        x, y = normalize_employer(a), normalize_employer(b)
        if x and y and x == y:
            return True
        x2 = normalize_employer(_PAREN_RE.sub(" ", str(a or "")))
        y2 = normalize_employer(_PAREN_RE.sub(" ", str(b or "")))
        return bool(x2 and y2 and x2 == y2)
    except Exception:
        return False


def _dedupe_candidates(mentioned):
    """Collapse normalize-equal registry candidates to the shortest raw
    name (red-team 2026-09-17).

    "Samsara" and "Samsara Labs" are the same company under the
    normalizer (Labs is a legal suffix); naming it once keeps the parse
    payload exact — a substring-named text never drags a superstring
    duplicate into the authorized set.
    """
    by_norm = {}
    for emp in mentioned or ():
        n = normalize_employer(emp)
        if n not in by_norm or len(str(emp)) < len(str(by_norm[n])):
            by_norm[n] = emp
    return set(by_norm.values())


def _canonical_binding_employer(role_context) -> str:
    """Independent canonical employer binding from the role context.

    This is the employer as bound by the application context (the queue
    lead being processed) — a different input from the bank entry's scope
    text and from the registry. A legacy free-text employer scope only
    authorizes when this binding corroborates the requesting employer.
    Returns "" when no binding was supplied.
    """
    try:
        rctx = role_context or {}
        return str(rctx.get("company") or rctx.get("employer") or "")
    except Exception:
        return ""


def _parse_free_scope(text: str, registry=None):
    """Parse a free-text scope/employer_scope string.

    Returns one of:
      ("role", <role_id>)             — role_id <ID> only
      ("employers-exact", {names})    — restricted to the named employers;
                                        attribution AND resolution both use
                                        exact normalized equality (never
                                        prefix-guessing)
      ("global", None)                — explicit ALL-leads standing rule
      (None, None)                    — no parseable scoping (legacy)
    Fail-safe: never raises.

    ``registry`` is an optional pre-snapshotted set of employer names
    (compound-growth reuse sessions). When None, known_employers() is
    consulted once, exactly as before. A snapshot is a name-detection aid
    only — never authority — so staleness degrades toward abstain.
    """
    try:
        t = str(text or "").strip()
        if not t:
            return None, None
        # Normalize slash-joined names ("Flynn Group / Panera") to spaces so
        # the phrase/ONLY extractors see one continuous name.
        t = re.sub(r"\s*/\s*", " ", t)
        m = _ROLEID_RE.search(t)
        if m:
            return "role", m.group(1)
        if _ALL_LEADS_RE.search(t):
            return "global", None
        only = _ONLY_RE.search(t)
        if only:
            # Guard: "<Name> ONLY" must name a real employer — a bare legal
            # suffix ("Inc. ONLY") normalizes to nothing and must NOT become
            # the scope; fall through to phrase extraction instead.
            if normalize_employer(only.group(1)):
                return "employers-exact", {only.group(1).strip()}
        # Employer names mentioned in a scoping sentence ("Samsara
        # applications; ...", "applies to ... (all listed applications)",
        # "(EliseAI Revenue Operations Manager, Base Power ...)") restrict
        # the entry to the employers it names. A caller-supplied registry
        # snapshot replaces the per-call known_employers() read (compound
        # growth: one registry load per reuse session, not per resolution).
        known = (set(registry) if registry is not None
                 else (_known_employers() or set()))
        mentioned = set()
        for phrase in _CAP_PHRASE_RE.findall(t):
            # The extractor greedily folds a leading preposition into the
            # phrase ("For Circle Medical"); try the phrase with and
            # without a leading stop-word (red-team 2026-09-17).
            cands = {phrase}
            stripped = _STOP_LEAD_RE.sub("", phrase).strip()
            if stripped:
                cands.add(stripped)
            for cand in cands:
                for emp in known:
                    if _norm_eq(cand, emp):
                        mentioned.add(emp)
        if mentioned:
            return "employers-exact", _dedupe_candidates(mentioned)
        # Token fallback (red-team 2026-09-17): lowercase/digit registry
        # names ("addepar1", "quantumspacellc") never surface as capitalized
        # phrases. Try raw tokens — boilerplate stop-words excluded so a
        # stray "for"/"all" can never scope an entry on its own.
        for tok in _TOKEN_RE.findall(t):
            if len(tok) < 3 or _STOP_WORDS_RE.match(tok):
                continue
            for emp in known:
                if _norm_eq(tok, emp):
                    mentioned.add(emp)
                    break
        if mentioned:
            return "employers-exact", _dedupe_candidates(mentioned)
        # Fallback (red-team 2026-09-17): the phrase extractor can miss
        # names wrapped in punctuation ("Samsara (Holding)"). If the whole
        # scope text normalizes to a known employer, that IS the scope —
        # this keeps a valid scoped entry from degrading to legacy.
        for emp in known:
            if _norm_eq(t, emp):
                return "employers-exact", {emp}
        return None, None
    except Exception:
        return None, None


# ----------------------------------------------------------------- expiry

_EXPIRY_FIELDS = ("expires", "expiry", "valid_until", "expires_at")


def _read_expiry(entry) -> str:
    try:
        if isinstance(entry, dict):
            for f in _EXPIRY_FIELDS:
                v = entry.get(f)
                if v:
                    return str(v).strip()
    except Exception:
        pass
    return ""


def _is_expired(expiry: str) -> bool:
    """True when a recorded expiry is parseable and in the past.

    Unparseable expiry text is IGNORED (not invented as expired) — the
    entry's other metadata still governs. Fail-safe: error -> not expired.
    """
    try:
        if not expiry:
            return False
        s = expiry.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < datetime.now(timezone.utc)
    except Exception:
        return False


# --------------------------------------------------------------- resolution

#: Banked REFUSAL values -- directives that an attestation/answer must never
#: be auto-completed, not answers. Canonical home of the pattern the ARM1
#: adjudication (2026-09-18, J-20260918-0324-veri-1823) established:
#: "a banked REFUSAL value ('DO NOT CERTIFY ... route to PERSONAL
#: TAKEOVER') is not a resolution -- it confirms the attestation cannot be
#: auto-completed and the lead needs the applicant's personal takeover."
#: verify_retry.py keeps its own copy for the promotion-gate comment trail;
#: the resolver is authoritative -- resolve() abstains on these, so no
#: consumer can render, fill, or compare a refusal as an answer.
REFUSAL_VALUE_RE = re.compile(
    r"do not certify|personal takeover|do not auto|never preauthorized|keep parked",
    re.I)


def _source(entry) -> str:
    try:
        if isinstance(entry, dict):
            p = entry.get("provenance") or entry.get("source") or ""
            return str(p).strip()[:500]
    except Exception:
        pass
    return ""


def _abstain(key, reason, entry=None) -> Resolution:
    return Resolution(key=key, status=STATUS_ABSTAIN, scope=SCOPE_LEGACY_UNKNOWN,
                      source=_source(entry), reason=reason)


def resolve(key, entry, employer=None, role_context=None, registry=None) -> Resolution:
    """Resolve one bank key for one employer/role. Fail-closed: any error
    returns ABSTAIN. Never invents an answer.

    The employer and role_context are snapshotted to plain strings at
    entry, so caller-side mutation of the role dict mid-call cannot alter
    the resolution (TOCTOU-hardened).

    role_context may carry "company"/"employer": the canonical employer
    binding from the application context (the queue lead being processed).
    A legacy free-text employer scope only authorizes when this binding
    independently corroborates the requesting employer (red-team round 9:
    registry + scope text + requester agreeing is self-referential — the
    registry is never authority on its own).

    registry is an optional pre-snapshotted set of employer names used for
    LEGACY free-text name detection instead of the per-call
    known_employers() read (compound growth: answer_reuse.ReuseSession
    loads it once per session). None (default) preserves the exact
    historical behavior.
    """
    try:
        employer = str(employer or "")
        rc = {"role_id": "", "company": ""}
        try:
            rctx = role_context or {}
            rc["role_id"] = str(rctx.get("role_id") or "")
            _ce = rctx.get("company") or rctx.get("employer") or ""
            rc["company"] = str(_ce)
        except Exception:
            pass
        return _resolve(key, entry, employer, rc, registry=registry)
    except Exception as exc:  # pragma: no cover — total function
        return Resolution(key=str(key), status=STATUS_ABSTAIN,
                          reason=f"resolver error (fail-closed): {exc}")


def _resolve(key, entry, employer, role_context, registry=None):
    key = str(key or "")
    scope = _machine_scope(entry)
    value = _bank_value(entry).strip()
    source = _source(entry)
    expiry = _read_expiry(entry)

    if entry is None or value == "":
        return _abstain(key, "no banked answer for this key", entry)

    # --- refusal directives are not answers --------------------------------
    # 2026-09-18 (J-20260918-0900-gate-1990): a banked REFUSAL value ("DO
    # NOT CERTIFY ... route to PERSONAL TAKEOVER") is the applicant's standing
    # personal-takeover law, not an answer the pipeline may render, fill,
    # or compare. The resolver abstains so every consumer (brief render,
    # consistency guard, draft fill, explicit-launch vetting) treats the
    # key as unanswerable-by-agent. Fail-closed: the no-AI boundary
    # (never auto-agree) is the applicant's law -- this strengthens it, never
    # weakens it. A genuine the applicant authorization contains none of the
    # directive phrases and still resolves.
    if REFUSAL_VALUE_RE.search(value):
        return _abstain(key, "banked answer is a personal-takeover refusal "
                             "(never auto-completed); the applicant's personal "
                             "takeover required", entry)

    # --- residence authorization gate ------------------------------------
    # 2026-09-18 (P0 residence repair): current_zip / current_city resolve
    # ONLY from the applicant's explicit, global-scoped, own-words bank entry. A
    # residence value without his authorization abstains here, before any
    # scope branch — it is never rendered into a brief, filled into a
    # form, or compared by a guard. Fail-closed: the P0 residence rule.
    if str(key or "") in _RESIDENCE_KEYS and not is_operator_authorized_residence(
            key, entry):
        return _abstain(key, "residence key without the applicant's explicit "
                             "global own-words authorization; never used",
                        entry)

    # --- machine-readable scope governs first ---------------------------
    if scope == "ambiguous":
        return _abstain(key, "scope is 'ambiguous' (quarantined); "
                             "never used without the applicant's explicit --scope",
                        entry)
    if _is_expired(expiry):
        return _abstain(key, f"banked authority expired ({expiry})", entry)
    if scope == SCOPE_GLOBAL:
        return Resolution(key=key, status=STATUS_RESOLVED, value=value,
                          scope=SCOPE_GLOBAL, source=source, expiry=expiry)
    if scope.startswith("employer:"):
        scoped_name = scope[len("employer:"):].strip()
        if employers_match(employer, scoped_name):
            return Resolution(key=key, status=STATUS_RESOLVED, value=value,
                              scope=scope, source=source, expiry=expiry)
        return _abstain(
            key, f"scoped to employer {scoped_name!r}; requesting employer "
                 f"is {employer!r} — a scoped answer never crosses employers",
            entry)

    # --- legacy / free-text scope parsing ------------------------------
    parsed = (None, None)
    if isinstance(entry, dict):
        parsed = _parse_free_scope(entry.get("employer_scope")
                                   or entry.get("scope"),
                                   registry=registry)
    kind, payload = parsed
    req_role_id = None
    try:
        req_role_id = (role_context or {}).get("role_id")
    except Exception:
        req_role_id = None

    if kind == "role":
        if req_role_id and str(req_role_id).strip() == str(payload).strip():
            return Resolution(key=key, status=STATUS_RESOLVED, value=value,
                              scope=f"role:{payload}", source=source,
                              expiry=expiry)
        return _abstain(
            key, f"scoped to role_id {payload!r}; requesting role is "
                 f"{req_role_id!r} — a per-role answer never crosses roles",
            entry)
    if kind == "employers-exact":
        # Legacy free-text attribution: EXACT normalized equality on both
        # sides (red-team 2026-09-17). The prefix rule lives only in the
        # explicit machine "employer:<name>" path; here a text naming
        # "Apple" never authorizes "Apple Pie Inc." — ambiguity abstains.
        names = sorted(payload or set())
        if any(_norm_eq(employer, n) for n in names):
            # Structural close (red-team round 9): the registry is a
            # name-detection aid, never authority. Registry + scope text +
            # requester agreeing is self-referential — a poisoned registry
            # could make any scope text "match" any requester. The legacy
            # scope only authorizes when an INDEPENDENT canonical employer
            # binding from the role context (the queue lead being
            # processed) corroborates the requesting employer.
            canon = _canonical_binding_employer(role_context)
            if canon and _norm_eq(employer, canon):
                return Resolution(
                    key=key, status=STATUS_RESOLVED, value=value,
                    scope="employers-exact:" + "|".join(names),
                    source=source, expiry=expiry)
            return _abstain(
                key, f"legacy employer scope names {names} and the "
                     f"requester is {employer!r}, but no independent "
                     f"canonical employer binding corroborates it "
                     f"(registry is never authority on its own) — "
                     f"abstaining",
                entry)
        return _abstain(
            key, f"scoped to employer(s) {names}; requesting employer is "
                 f"{employer!r} — a scoped answer never crosses employers",
            entry)
    if kind == "global":
        # Explicit ALL-leads standing rule — still flagged legacy so it
        # stays visible, but it is the applicant's named standing rule, not a
        # silent broadening. Structural default-deny (red-team round 9):
        # the key must ALSO be on the explicit safe-legacy allowlist (or
        # preauthorized) — an unlisted key with ALL-leads text abstains.
        if (key not in _SAFE_LEGACY_KEYS
                and key not in PREAUTHORIZED_ATTESTATION_KEYS):
            return _abstain(
                key, "ALL-leads scope text, but the key is not on the "
                     "explicit safe-legacy allowlist — abstain-by-default "
                     "(sensitivity-unknown); classify it before use",
                entry)
        res = Resolution(key=key, status=STATUS_LEGACY, value=value,
                         scope="legacy:ALL-leads-standing-rule",
                         source=source, expiry=expiry, legacy=True,
                         reason="pre-scope entry; scope text says ALL leads "
                                "(standing rule) — flagged, never silent")

    else:
        # --- flagged legacy: scope unestablished, never broadened --------
        if _is_sensitive_key(key):
            if key in PREAUTHORIZED_ATTESTATION_KEYS:
                return Resolution(
                    key=key, status=STATUS_RESOLVED, value=value,
                    scope="standing-preauthorization",
                    source=source or _PREAUTH_SOURCE,
                    expiry=expiry,
                    reason="the applicant's explicit standing pre-authorization "
                           "(named, not silently global)")
            return _abstain(
                key, "consent/attestation-class key with unestablished scope "
                     "— abstain-by-default; only the applicant's explicit standing "
                     "pre-authorization (or a matching employer scope) can "
                     "authorize it",
                entry)
        # Structural default-deny (red-team round 9): a legacy key the
        # sensitivity heuristic does NOT recognize is still
        # sensitivity-unknown — it resolves as flagged legacy ONLY on the
        # explicit safe list. This is the "Shadow Consent" close: a future
        # data_retention_agreement-style key can never silently render.
        if key not in _SAFE_LEGACY_KEYS:
            return _abstain(
                key, "legacy-scope key is not on the explicit safe-legacy "
                     "allowlist — abstain-by-default (sensitivity-unknown); "
                     "classify it before use",
                entry)
        res = Resolution(key=key, status=STATUS_LEGACY, value=value,
                         scope=SCOPE_LEGACY_UNKNOWN, source=source,
                         expiry=expiry, legacy=True,
                         reason="pre-scope entry; no employer/role scoping "
                                "established — explicit legacy, not global")
    # Sensitive + flagged legacy always abstains, even when the free text
    # said ALL leads (consent is per-instance; the applicant's standing
    # pre-authorization list is the only exception, handled above).
    if _is_sensitive_key(key) and key not in PREAUTHORIZED_ATTESTATION_KEYS:
        return _abstain(
            key, "consent/attestation-class key; scope metadata is legacy — "
                 "abstain-by-default",
            entry)
    return res


def resolve_bank(answers: dict, employer=None, role_context=None,
                 registry=None) -> dict:
    """Resolve every key of an answers dict for one employer/role.

    Returns {key: Resolution}. Fail-safe: never raises.

    registry is an optional pre-snapshotted employer-name set shared by
    every key in the batch (compound growth: one registry load per batch,
    not per key). None (default) preserves the exact historical behavior.
    """
    out = {}
    try:
        items = (answers or {}).items()
    except Exception:
        return out
    for k, v in items:
        out[str(k)] = resolve(k, v, employer, role_context, registry=registry)
    return out


# --------------------------------------------------- identity type checks

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+?[\d\s().-]{7,25}$")
#: Names must contain at least one Unicode letter (red-team round 9): the
#: old [\w...] class also matched pure digits/underscores ("123").
_NAME_RE = re.compile(r"^(?=.*[^\W\d_])[\w'’.,\- ]{1,80}$", re.UNICODE)


def validate_identity_field(key: str, value) -> tuple:
    """Type-validate an identity value before POST assembly.

    Returns (ok: bool, reason: str). Only shapes are checked — the truth
    of the value is the applicant's banked data, never derived here.

    Strict (red-team round 9): a non-string value is REJECTED, never
    coerced with str(). Dict-form metadata extraction happens in _resolve
    via _bank_value — by the time a value reaches this check it must
    already be a string.
    """
    try:
        if not isinstance(value, str):
            return False, (f"{key}: non-string identity value "
                           f"({type(value).__name__}) — rejected, not coerced")
        v = value.strip()
        k = str(key or "").lower()
        if not v:
            return False, f"{key}: empty identity value"
        if k == "email":
            return (True, "") if _EMAIL_RE.match(v) else (
                False, f"email failed shape check: {v[:40]!r}")
        if k == "phone":
            return (True, "") if _PHONE_RE.match(v) else (
                False, f"phone failed shape check: {v[:40]!r}")
        if k in ("first_name", "last_name"):
            return (True, "") if _NAME_RE.match(v) else (
                False, f"{key} failed shape check: {v[:40]!r}")
        # location and other identity facts: non-empty string only
        return True, ""
    except Exception as exc:
        return False, f"{key}: validation error (fail-closed): {exc}"


def resolve_identity(key, entry, employer=None) -> Resolution:
    """Resolve + type-validate one identity field for POST assembly.

    Authorized when the answer is resolved, OR when it is flagged-legacy
    and NON-sensitive (plain-string standing facts like email/phone/name:
    the [LEGACY] explicitness lives at the brief layer; POST assembly
    needs the validated value). Anything else — missing, expired,
    out-of-scope, wrong type, or sensitive-class legacy — fails closed to
    ABSTAIN with the identity_type reason. Nothing malformed ever reaches
    a payload.
    """
    res = resolve(key, entry, employer=employer,
                # The application context (the lead being submitted) is the
                # canonical employer binding: identity assembly always runs
                # against a concrete role, so the binding is present.
                role_context={"company": employer})
    if res.status == STATUS_ABSTAIN:
        return res
    if res.status == STATUS_LEGACY and _is_sensitive_key(key):
        return _abstain(key, "identity key is consent/attestation-class "
                             "with unestablished scope — abstain-by-default",
                        entry)
    ok, reason = validate_identity_field(key, res.value)
    if not ok:
        return _abstain(key, f"identity_type: {reason}", entry)
    # Non-sensitive legacy promotes to resolved (keeping legacy=True so the
    # explicitness is never dropped); sensitive-class legacy already
    # abstained above.
    if res.status == STATUS_LEGACY:
        res = Resolution(key=res.key, status=STATUS_RESOLVED,
                         value=res.value, scope=res.scope,
                         source=res.source, expiry=res.expiry,
                         legacy=True, reason=res.reason)
    return res
