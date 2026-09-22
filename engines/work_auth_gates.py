"""work_auth_gates.py — territorial work-auth gate taxonomy + pre-staging screen.

Territorial work-auth gate taxonomy + pre-staging screen (ported
2026-09-21 from the production pipeline's FIX 3):

Two jobs, one module:

1. CANONICAL GATE TAXONOMY. The same territorial work-auth blocker was
   emitted under ad-hoc per-country gate names (singapore_work_auth_unverified,
   india_work_auth_no, uk_work_auth_unverified, ...), tripping the
   fanout gate's gate:new_type:* detector on every new country. The ONE
   canonical gate is WORK_AUTH_GATE ("work_auth_unverified"); the
   country/jurisdiction rides in the event details, never in the name.
   WORK_AUTH_GATE_LEGACY_MAP maps every observed ad-hoc variant to the
   canonical name so historical telemetry stays queryable. The emission
   choke (log_event.log) normalizes at write time, preserving the original
   in details["gate_aliased_from"] per the ENGINE-BRIDGE pattern; historical
   rows are never rewritten (append-only log).

2. pre_staging_workauth_screen(location, bank=None): pure prefilter for
   staging_ingest.triage(), same shape as
   title_triage.pre_staging_title_gate — a pure function on the normalized
   location string, run at triage time BEFORE any verify/packet cycles.

Standing facts used (READ-ONLY — nothing here banks, invents, re-derives,
or changes any fact). Example posture from the operator's own answer bank:
  - the applicant is a US citizen (us_citizen_resident=Yes, operator's words).
  - the applicant holds US-only work authorization (us_work_auth=Yes,
    operator's words).
  - never-infer work auth: a country's authorization is resolved ONLY by a
    banked <country>_work_auth key with the applicant's-own-words provenance.
  - configured relocation willingness is about RELOCATION and is NEVER
    consulted for work authorization — conflating the two is the defect
    class this module refuses to repeat.

Park rule: the location explicitly names a non-US country -> the lead parks
at triage with the canonical gate UNLESS a banked <country>_work_auth=YES
with own-words provenance establishes authorization for that country.
The park cites EITHER the banked country NO (operator's words) OR, where no
country fact resolves, the standing US-only posture (us_work_auth=Yes,
operator's words). EVERYTHING else flows through unchanged (never-infer):
US locations, ambiguous/unmapped locations, and remote/remote-in-territory
roles. Configured relocation willingness is never consulted.
"""

import re

# ---------------------------------------------------------------------------
# 1. Canonical taxonomy
# ---------------------------------------------------------------------------

WORK_AUTH_GATE = "work_auth_unverified"

# Country slug -> display name, used for details + legacy-name extraction.
COUNTRY_DISPLAY = {
    "singapore": "Singapore",
    "uk": "UK",
    "ireland": "Ireland",
    "australia": "Australia",
    "india": "India",
    "japan": "Japan",
    "germany": "Germany",
    "newzealand": "New Zealand",
    "mexico": "Mexico",
    "france": "France",
}

# Every ad-hoc per-country gate name observed in production telemetry
# -> canonical.
# Historical queries against the old names stay answerable via
# details["gate_aliased_from"], written by the emission-time normalizer.
WORK_AUTH_GATE_LEGACY_MAP = {
    "singapore_work_auth_unverified": WORK_AUTH_GATE,
    "uk_work_auth_unverified": WORK_AUTH_GATE,
    "ireland_work_auth_unverified": WORK_AUTH_GATE,
    "australia_work_auth_unverified": WORK_AUTH_GATE,
    "india_work_auth_no": WORK_AUTH_GATE,
    "japan_work_auth_unverified": WORK_AUTH_GATE,
    "germany_work_auth_unverified": WORK_AUTH_GATE,
    "newzealand_work_auth_unverified": WORK_AUTH_GATE,
    "mexico_work_auth_unverified": WORK_AUTH_GATE,
}

# Future-proofing: any {slug}_work_auth_(unverified|no) minted by a future
# country collapses to the canonical name too, so a new territory never
# trips gate:new_type:* again.
_ADHOC_WORK_AUTH_RE = re.compile(r"^([a-z][a-z0-9]*)_work_auth_(unverified|no)$")


def canonicalize_work_auth_gate(name):
    """Map an ad-hoc per-country work-auth gate name to the canonical one.

    Returns (canonical_name, country_display_or_None). Names that are not
    work-auth gates pass through unchanged (never touch other vocabularies).
    """
    if not isinstance(name, str) or not name:
        return name, None
    if name == WORK_AUTH_GATE:
        return name, None
    if name in WORK_AUTH_GATE_LEGACY_MAP:
        slug = name.rsplit("_work_auth_", 1)[0]
        return WORK_AUTH_GATE, COUNTRY_DISPLAY.get(slug, slug)
    m = _ADHOC_WORK_AUTH_RE.match(name)
    if m:
        slug = m.group(1)
        return WORK_AUTH_GATE, COUNTRY_DISPLAY.get(slug, slug)
    return name, None


# ---------------------------------------------------------------------------
# 2. Pre-staging territorial screen
# ---------------------------------------------------------------------------

# Deterministic location -> country-slug tokens. A slug
# only ever feeds a READ-ONLY bank lookup or the US-only posture rule
# below — it never invents authorization by itself.
_COUNTRY_LOCATION_PATTERNS = {
    "singapore": re.compile(r"\bsingapore\b", re.I),
    "uk": re.compile(r"\buk\b|\blondon\b|\bengland\b|\bscotland\b|\bwales\b",
                     re.I),
    "ireland": re.compile(r"\bireland\b|\bdublin\b", re.I),
    "australia": re.compile(
        r"\baustralia\b|\bsydney\b|\bmelbourne\b|\bbrisbane\b|\bperth\b|"
        r"\bcanberra\b", re.I),
    "india": re.compile(
        r"\bindia\b|\bbengaluru\b|\bbangalore\b|\bmumbai\b|\bdelhi\b|"
        r"\bhyderabad\b|\bchennai\b|\bpune\b|\bkolkata\b", re.I),
    "japan": re.compile(r"\bjapan\b|\btokyo\b|\bosaka\b|\bkyoto\b", re.I),
    "germany": re.compile(r"\bgermany\b|\bberlin\b|\bmunich\b|\bhamburg\b",
                           re.I),
    "newzealand": re.compile(r"\bnew zealand\b|\bnewzealand\b|\bauckland\b",
                             re.I),
    "mexico": re.compile(r"\bmexico\b", re.I),
    "canada": re.compile(r"\bcanada\b|\btoronto\b|\bvancouver\b|\bmontreal\b",
                         re.I),
    "france": re.compile(
        r"\bfrance\b|\bparis\b|\blyon\b|\bmarseille\b", re.I),
    "united states": re.compile(r"\bunited states\b", re.I),
}

# US-location detection, checked BEFORE country resolution so a US postal
# abbreviation never misfires as a foreign country ("Napa, CA" is Napa,
# California — not Canada). Fixed lists only; no inference.
_US_STATE_ABBR = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI "
    "MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT "
    "VA WA WV WI WY".split())
_US_STATE_NAMES = frozenset([
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "south carolina", "north dakota", "south dakota",
    "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "tennessee", "texas", "utah", "vermont", "virginia", "west virginia",
    "washington", "wisconsin", "wyoming", "district of columbia",
])
_US_RE = re.compile(r"\b(united states|usa|u\.s\.a?\.?)\b", re.I)
_US_TRAILING_ABBR_RE = re.compile(r",\s*([a-z]{2})\s*$", re.I)


def _is_us_location(loc):
    """True when the location string explicitly marks the US.

    Matches "United States"/"USA"/"U.S.", a trailing ", XX" postal
    abbreviation in the fixed 50-state+DC list ("Napa, CA"), or a full
    state name ("Austin, Texas"). Anything else -> False (never-infer).
    """
    if not loc:
        return False
    if _US_RE.search(loc):
        return True
    m = _US_TRAILING_ABBR_RE.search(loc)
    if m and m.group(1).upper() in _US_STATE_ABBR:
        return True
    low = loc.lower()
    return any(re.search(r"\b%s\b" % re.escape(s), low)
               for s in _US_STATE_NAMES)

# Remote-flavored locations are NEVER screened here: remote-in-territory
# handling stays exactly as today (prescreen gates on the observed form
# question, never on the location string) — this was flagged as a risk, not
# an approved change.
_REMOTE_FLAVOR_RE = re.compile(r"\bremote\b|\bhybrid\b|\bwork from home\b|\bwfh\b",
                               re.I)

_OWN_WORDS_RE = re.compile(r"own words", re.I)


def _country_for_location(location):
    """Country slug for a location string, or None when unresolvable.

    Slugs only feed read-only bank lookups and the US-only posture rule;
    an unresolvable location is never-infer (flow through).
    """
    loc = (location or "").strip()
    if not loc:
        return None
    for slug, pat in _COUNTRY_LOCATION_PATTERNS.items():
        if pat.search(loc):
            return slug
    return None


def _bank_value_and_provenance(bank, key):
    """Read-only bank lookup. Returns (value, provenance_source)."""
    if not isinstance(bank, dict):
        return None, ""
    answers = bank.get("answers")
    rec = answers.get(key) if isinstance(answers, dict) else None
    value, prov = None, ""
    if isinstance(rec, dict):
        value = rec.get("value")
        prov = rec.get("provenance") or ""
    elif rec is not None:
        value = rec
    preg = bank.get("_provenance")
    if isinstance(preg, dict) and isinstance(preg.get(key), dict):
        src = preg[key].get("source") or ""
        if not prov:
            prov = src
    return value, prov


def pre_staging_workauth_screen(location, bank=None):
    """Screen a lead's location against the operator's STANDING work-auth facts.

    Returns (keep, note, gate, country, basis):
      keep=True  -> ingest normally (flow through UNCHANGED).
      keep=False -> park at triage: reject to the work-auth reject ledger
                    (recoverable, auditable, zero queue clutter), never
                    ingested — so zero verify/packet cycles are burned.
    note carries the lowercase `needs_input` token (verify_retry's
    park-guard matches only the lowercase token), the canonical gate name,
    and the standing-fact citation. gate is WORK_AUTH_GATE when parked,
    else None. basis is the cited standing fact.

    Pure function: read-only against the bank, no I/O, no invention.
    Configured relocation-willingness keys are deliberately never consulted —
    relocation willingness is not work authorization.
    """
    loc = (location or "").strip()
    # Remote/remote-in-territory: untouched — prescreen owns that lane.
    if _REMOTE_FLAVOR_RE.search(loc):
        return True, "remote-flavored location — flow through unchanged", \
            None, None, ""
    if not loc:
        # Blank location: never-infer -> flow through unchanged.
        return True, "blank location — flow through unchanged (never-infer)", \
            None, None, ""
    if _is_us_location(loc):
        # US location: the applicant's work authorization covers the US.
        return True, "US location — flow through unchanged", None, None, ""
    country = _country_for_location(loc)
    if country is None:
        # Unresolvable location (unknown city, ambiguous string): never-infer.
        return True, ("no resolving standing fact for location %r — "
                      "flow through unchanged (never-infer)") % (loc,), \
            None, None, ""
    if country == "united states":
        return True, "US location — flow through unchanged", None, None, ""
    display = COUNTRY_DISPLAY.get(country, country)
    bank_key = "%s_work_auth" % country.replace(" ", "")
    value, provenance = _bank_value_and_provenance(bank, bank_key)
    vnorm = value.strip().upper() if isinstance(value, str) else ""
    if vnorm == "YES":
        # A banked own-words YES establishes authorization for the country.
        return True, ("banked %s=YES — flow through unchanged" % bank_key), \
            None, None, ""
    if vnorm == "NO" and _OWN_WORDS_RE.search(provenance or ""):
        # Banked country NO with the applicant's-own-words provenance: park citing it.
        basis = "%s=NO (%s)" % (bank_key, provenance.strip()[:160])
        note = ("needs_input | gate=%s | country=%s | standing fact: %s | "
                "park at triage, zero verify/packet cycles burned"
                % (WORK_AUTH_GATE, display, basis))
        return False, note, WORK_AUTH_GATE, country, basis
    # Explicit foreign country, no resolving country fact: fall back to the
    # standing US-only authorization posture (the operator's explicit
    # requirement: territorial roles park unless a standing fact establishes
    # that country's authorization). The posture itself must be an own-words
    # fact — never invented.
    us_value, us_provenance = _bank_value_and_provenance(bank, "us_work_auth")
    us_norm = us_value.strip().upper() if isinstance(us_value, str) else ""
    if us_norm == "YES" and _OWN_WORDS_RE.search(us_provenance or ""):
        basis = ("us_work_auth=YES (%s) — US-only authorization posture; "
                 "no standing fact establishes %s work authorization"
                 % (us_provenance.strip()[:160], display))
        note = ("needs_input | gate=%s | country=%s | standing fact: %s | "
                "park at triage, zero verify/packet cycles burned"
                % (WORK_AUTH_GATE, display, basis))
        return False, note, WORK_AUTH_GATE, country, basis
    # Posture fact missing or unprovenanced: never invent it — flow through.
    return True, ("no resolving standing fact for location %r — "
                  "flow through unchanged (never-infer)") % (loc,), \
        None, None, ""
