#!/usr/bin/env python3
"""Inbox outcome listener — classifies ATS/employer mail and links it to ledger entries.

Backfills employer responses (auto-acks, rejections, interview invites, info
requests, offers) against the application ledger. Read-only by default; --live appends
`employer_response` telemetry events via log_event.py (append-only, safe).

Classification is rule-based and fully auditable: every label links to the
source message. Spot-check the report before trusting precision.

The mail source is pluggable (see MailSource): the public edition ships a
MaildirReader that reads .eml files from a local directory — no account
coupling. Live events carry the message's real receipt date
(details.message_date, preferred for latency math), a quoted-text excerpt
for downstream quote-only extraction, and the company's linked fit_score
so score→conversion becomes measurable.

LinkedIn DMs/InMails and Indeed employer messages are unwatched channels
(no read API); the listener catches their EMAIL NOTIFICATIONS via
sender/subject tripwires (linkedin_tripwire / indeed_tripwire) and tags
them so the channel is attributable in outcome analytics.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

def _load_config():
    try:
        return json.load(open(os.path.join(PIPE, "keel.config.json")))
    except Exception:
        return {}


PIPE = os.environ.get("KEEL_HOME", os.path.expanduser("~/keel"))
LEDGER = os.path.join(PIPE, "data", "application-ledger.json")
LOG_EVENT = os.path.join(PIPE, "engines", "log_event.py")

ATS_SENDERS = [
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
    "builtin.com", "applytojob.com", "jobvite.com", "icims.com",
    "smartrecruiters.com", "workable.com", "breezy.hr", "jobs.lever.co",
    "boards.greenhouse.io", "taleo.net",
]

AUTO_ACK_SUBJECT = [
    r"thank you for applying", r"thanks for applying",
    r"application.*received", r"received your application",
    r"confirming receipt", r"successfully submitted",
    r"your application for",
]
REJECTION_PATTERNS = [
    r"not moving forward", r"moving forward with other",
    r"decided not to proceed", r"not selected", r"position has been filled",
    r"pursue other candidates", r"no longer under consideration",
    r"we will not be moving", r"decided to pursue",
]
OFFER_PATTERNS = [
    r"\boffer letter\b",
    r"\bextending an offer\b", r"\bextend(?:ed|s)? (?:you |an )?offer\b",
    r"\bpleased to offer\b", r"\bdelighted to offer\b", r"\bexcited to offer\b",
    r"\bthrilled to offer\b", r"\bverbal offer\b",
    r"\bformal offer\b",
]
INVITE_SUBJECT = r"interview"
INVITE_SCHEDULE = r"schedul|invit|confirm|request|availability|calendar"


def classify(subject, body, from_email):
    """Order matters: ack-subject is decisive; invite needs scheduling language
    in the subject (ack bodies routinely mention 'interview process')."""
    subj = subject or ""
    text = f"{subj} {body or ''}"
    for p in AUTO_ACK_SUBJECT:
        if re.search(p, subj, re.IGNORECASE):
            return "AUTO_ACK", f"subject:{p}"
    for p in AUTO_ACK_SUBJECT:
        if re.search(p, body or "", re.IGNORECASE):
            return "AUTO_ACK", f"body:{p}"
    for p in REJECTION_PATTERNS:
        if re.search(p, text, re.IGNORECASE):
            return "REJECTION", p
    # OFFER before INTERVIEW_INVITE: offer follow-ups can carry scheduling
    # language ("let's schedule a call to discuss the offer") without the
    # word "interview" in the subject. Order matters — decisive first.
    # Guard: "extend an offer to another candidate" is a rejection of US —
    # it must not classify as our OFFER (none of the rejection patterns
    # catch that phrasing).
    _offer_to_other = re.search(
        r"\boffer\b.{0,40}\b(?:another|a different|other)\s+candidate\b",
        text, re.IGNORECASE)
    if not _offer_to_other:
        for p in OFFER_PATTERNS:
            if re.search(p, text, re.IGNORECASE):
                return "OFFER", p
    if (re.search(INVITE_SUBJECT, subj, re.IGNORECASE)
            and re.search(INVITE_SCHEDULE, text, re.IGNORECASE)):
        return "INTERVIEW_INVITE", "subject-schedule"
    for p in [r"\bcodesignal\b", r"\bhackerrank\b", r"take-home",
              r"\bassessment\b", r"\bhirevue\b"]:
        if re.search(p, text, re.IGNORECASE):
            return "ASSESSMENT", p
    for p in [r"additional information", r"please provide", r"questionnaire",
              r"could you (share|confirm|clarify)", r"missing information"]:
        if re.search(p, text, re.IGNORECASE):
            return "INFO_REQUEST", p
    return "OTHER", None

COMPANY_PATTERNS = [
    r"thank you for applying to ([A-Z][\w&.\- ]+)",
    r"your application to ([A-Z][\w&.\- ]+)",
    r"applying (?:for|to).*? at ([A-Z][\w&.\- ]+)",
]


def norm_company(name):
    n = (name or "").lower().strip()
    n = re.sub(r"\b(inc|llc|ltd|corp|co|org|gmbh|pbc)\b\.?", "", n)
    n = re.sub(r"[^a-z0-9 ]", "", n)
    return re.sub(r"\s+", " ", n).strip()


def load_ledger():
    entries = json.load(open(LEDGER))
    idx = {}
    for e in entries:
        key = norm_company(e.get("company", ""))
        if key:
            idx.setdefault(key, []).append(e)
    submitted = [e for e in entries if e.get("status") == "SUBMITTED"]
    return entries, idx, submitted


# ---------------------------------------------------------------------------
# Mail source (pluggable). The public edition ships a MaildirReader that
# reads .eml files from a local directory — no account coupling.
# To use Gmail/IMAP/an API instead, subclass MailSource and pass it in.
# ---------------------------------------------------------------------------
class MailSource:
    """Yields message dicts: {id, subject, from, date, body_text}."""

    def triage(self, lookback_days=14, max_n=100):
        raise NotImplementedError

    def read(self, msg_id):
        raise NotImplementedError


class MaildirReader(MailSource):
    """Reads .eml files from a directory (default mail source)."""

    def __init__(self, maildir):
        self.maildir = maildir

    def _parse(self, path):
        import email
        from email import policy
        with open(path, "rb") as f:
            msg = email.message_from_binary_file(f, policy=policy.default)
        body = msg.get_body(preferencelist=("plain",))
        return {
            "id": os.path.basename(path),
            "subject": str(msg.get("Subject", "")),
            "from": str(msg.get("From", "")),
            "date": str(msg.get("Date", "")),
            "body_text": body.get_content() if body else "",
        }

    def triage(self, lookback_days=14, max_n=100):
        out = []
        try:
            names = sorted(os.listdir(self.maildir))
        except FileNotFoundError:
            return []
        for n in names[:max_n]:
            if n.lower().endswith((".eml", ".msg")):
                try:
                    out.append(self._parse(os.path.join(self.maildir, n)))
                except Exception:
                    continue
        return out

    def read(self, msg_id):
        return self._parse(os.path.join(self.maildir, msg_id))


def extract_company(msg, ledger_idx):
    subj = msg.get("subject", "") or ""
    frm = msg.get("from", {})
    frm_email = (frm.get("email", "") if isinstance(frm, dict) else str(frm)) or ""
    body = (msg.get("body_text", "") or "")[:1500]
    for pat in COMPANY_PATTERNS:
        for src in (subj, body):
            m = re.search(pat, src)
            if m:
                cand = norm_company(m.group(1))
                if cand in ledger_idx:
                    return cand, f"subject-pattern:{m.group(1).strip()}"
    # sender-domain fallback
    dom = frm_email.split("@")[-1].lower()
    for key in ledger_idx:
        if key and key.replace(" ", "") in dom.replace(".", ""):
            return key, f"sender-domain:{dom}"
    # token-overlap fallback: "Chime" <-> "Chime Financial, Inc"
    subj_n = norm_company(subj)
    best, best_score = None, 0.0
    for key in ledger_idx:
        if not key or len(key) < 4:
            continue
        if key in subj_n:
            return key, "subject-substring"
        kt, st = set(key.split()), set(subj_n.split())
        inter = {t for t in kt & st if len(t) >= 4}
        if inter:
            score = len(inter) / min(len(kt), len(st))
            if score > best_score:
                best, best_score = key, score
    if best and best_score >= 0.5:
        return best, f"token-overlap:{best_score:.2f}"
    return None, "unmatched"


def linked_fit_score(company_key, ledger_idx):
    """Predictive fit_score of the most recent SUBMITTED ledger row for a
    company, so employer_response events can carry it and score→conversion
    becomes measurable. Returns None when no SUBMITTED row carries a numeric
    score — never guessed."""
    rows = [e for e in ledger_idx.get(company_key, [])
            if e.get("status") == "SUBMITTED"]
    if not rows:
        return None

    def _datekey(e):
        ds = e.get("date_submitted") or e.get("submitted_at") or ""
        try:
            d = datetime.fromisoformat(
                str(ds).replace(" PDT", "-07:00").replace(" PST", "-08:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return (1, d)
        except Exception:
            return (0, datetime.min.replace(tzinfo=timezone.utc))

    rows.sort(key=_datekey)
    for e in reversed(rows):
        fs = e.get("fit_score")
        if isinstance(fs, (int, float)) and not isinstance(fs, bool):
            return fs
    return None


# ---------------------------------------------------------------------------
# LinkedIn / Indeed notification tripwires. LinkedIn DMs/InMails and Indeed
# employer messages live behind logins with no read API, so the listener
# only ever sees the EMAIL NOTIFICATIONS they send. These patterns catch
# that message-like traffic (and tag it) instead of dropping it with
# ledger_company=None. Deliberately scoped to message-like traffic: a
# blanket sender match would flood the triage cap with job alerts.
#
# Gmail-query form (for Gmail-backed MailSource subclasses):
#   LINKEDIN_SWEEP = from:"hit-reply@linkedin.com"
#                    OR from:"inmail-hit-reply@linkedin.com"
#                    OR from:"invitations@linkedin.com"
#                    OR subject:"You have a new message"
#                    OR subject:"replied to your message"
#                    OR subject:"invited you to connect"
#                    OR subject:"sent you an InMail" OR subject:"new InMail"
#   INDEED_SWEEP = from:indeed.com (subject:"new message"
#                  OR subject:"message from" OR subject:"sent you a message"
#                  OR subject:"responded to your application")
# ---------------------------------------------------------------------------
_LINKEDIN_SENDER_RE = re.compile(r"linkedin\.com", re.IGNORECASE)
_INDEED_SENDER_RE = re.compile(r"indeed\.com", re.IGNORECASE)
_LINKEDIN_MSG_SUBJECTS = ("you have a new message", "replied to your message",
                          "invited you to connect", "sent you an inmail",
                          "new inmail")
_INDEED_MSG_SUBJECTS = ("new message", "message from", "sent you a message",
                        "responded to your application")


def _msg_from_email(msg):
    frm = msg.get("from", "")
    return frm.get("email") if isinstance(frm, dict) else str(frm)


def is_linkedin_notification(from_email):
    """True when the message arrived via a LinkedIn notification address."""
    return bool(_LINKEDIN_SENDER_RE.search(from_email or ""))


def is_indeed_notification(from_email):
    """True when the message arrived via an Indeed notification address."""
    return bool(_INDEED_SENDER_RE.search(from_email or ""))


def linkedin_tripwire(msg):
    """True for message-like LinkedIn email notifications (unwatched
    channel — message content behind LinkedIn login is not readable; the
    notification itself is tagged so the channel is attributable)."""
    subj = (msg.get("subject") or "").lower()
    return (is_linkedin_notification(_msg_from_email(msg))
            and any(s in subj for s in _LINKEDIN_MSG_SUBJECTS))


def indeed_tripwire(msg):
    """True for message-like Indeed email notifications. Same residual gap
    as LinkedIn: content behind Indeed login is not readable without the
    user's authenticated session — no API, and scraping would violate ToS.
    Notifications are tagged so the channel is attributable in outcome
    analytics."""
    subj = (msg.get("subject") or "").lower()
    return (is_indeed_notification(_msg_from_email(msg))
            and any(s in subj for s in _INDEED_MSG_SUBJECTS))


def merge_triage_dedupe(msgs, kw):
    """Merge keyword-sweep hits into the triage list, deduping by message id.

    Uses its own local `triage_ids` set — it never touches the caller's
    persisted `seen` set. (Regression note: a previous version shadowed
    `seen` here, so the live loop skipped every message and --live logged
    nothing.)
    """
    triage_ids = {m["id"] for m in msgs}
    for m in kw:
        if m["id"] not in triage_ids:
            msgs.append(m)
            triage_ids.add(m["id"])
    return triage_ids


DATA_DIR = os.path.join(PIPE, "data")


def _default_mail_source():
    """Maildir under the workspace data dir (configure in keel.config.json)."""
    cfg = _load_config()
    maildir = cfg.get("maildir") or os.path.join(DATA_DIR, "mail")
    return MaildirReader(maildir)


def main(argv, mail_source=None):
    lookback = 7
    live = False
    out_dir = None
    for i, a in enumerate(argv):
        if a == "--lookback-days" and i + 1 < len(argv):
            lookback = int(argv[i + 1])
        if a == "--live":
            live = True
        if a == "--out" and i + 1 < len(argv):
            out_dir = argv[i + 1]

    out_dir = out_dir or os.path.join(PIPE, "hidden_files", "outcome-tracking")
    os.makedirs(out_dir, exist_ok=True)
    seen_path = os.path.join(out_dir, "seen-message-ids.json")
    seen = set()
    if os.path.exists(seen_path):
        try:
            seen = set(json.load(open(seen_path)))
        except Exception:
            seen = set()

    mail_source = mail_source or _default_mail_source()
    entries, ledger_idx, submitted = load_ledger()
    sender_q = " OR ".join(f"from:{s}" for s in ATS_SENDERS)
    query = f"newer_than:{lookback}d ({sender_q})"
    msgs = mail_source.triage(lookback_days=lookback)
    # keyword sweep for anything the sender filter missed, plus the
    # LinkedIn / Indeed notification tripwires (unwatched channels —
    # email tripwire only)
    def _kw_hit(m):
        text = f"{m.get('subject', '')} {m.get('body_text', '')}"
        return (re.search(r"thank you for applying|your application|interview",
                          text, re.I)
                or linkedin_tripwire(m) or indeed_tripwire(m))
    kw = [m for m in mail_source.triage(lookback_days=lookback) if _kw_hit(m)]
    merge_triage_dedupe(msgs, kw)

    results = []
    for m in msgs:
        if live and m["id"] in seen:
            continue
        full = mail_source.read(m["id"])
        frm = full.get("from", {})
        from_email = frm.get("email") if isinstance(frm, dict) else str(frm)
        label, pattern = classify(full.get("subject", ""),
                                  full.get("body_text", "") or full.get("snippet", ""),
                                  from_email)
        company_key, match_how = extract_company(full, ledger_idx)
        pipeline_related = bool(company_key) or any(
            s in (from_email or "") for s in ATS_SENDERS)
        frm = full.get("from", {})
        results.append({
            "message_id": m["id"],
            "date": full.get("date"),
            "from": from_email,
            "subject": full.get("subject"),
            "classification": label,
            "matched_pattern": pattern,
            "ledger_company": company_key,
            "match_how": match_how,
            "pipeline_related": pipeline_related,
            # LinkedIn / Indeed are unwatched channels; tag notification
            # emails so they're attributable even when ledger_company is None.
            "linkedin": is_linkedin_notification(from_email),
            "indeed": is_indeed_notification(from_email),
            # Body excerpt for the downstream hooks' quote-only extraction
            # (offer terms, rejection signals). Truncated at write time.
            "body_text": full.get("body_text", "") or full.get("snippet", ""),
        })

    # ack coverage: submitted >24h ago without an AUTO_ACK
    acked = {r["ledger_company"] for r in results
             if r["classification"] == "AUTO_ACK" and r["ledger_company"]}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    missing_acks = []
    for e in submitted:
        ds = e.get("date_submitted") or e.get("submitted_at") or ""
        try:
            # ledger dates are "YYYY-MM-DD HH:MM TZ" or ISO; be lenient
            d = datetime.fromisoformat(ds.replace(" PDT", "-07:00").replace(" PST", "-08:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if d < cutoff and norm_company(e.get("company", "")) not in acked:
            missing_acks.append({
                "company": e.get("company"), "title": e.get("title"),
                "resume_lane": e.get("resume_lane"),
                "date_submitted": ds,
            })

    # lane breakdown of outcomes
    lane_stats = {}
    for r in results:
        if not r["ledger_company"]:
            continue
        for e in ledger_idx.get(r["ledger_company"], []):
            lane = e.get("resume_lane", "?")
            s = lane_stats.setdefault(lane, {"AUTO_ACK": 0, "REJECTION": 0,
                                             "INTERVIEW_INVITE": 0, "INFO_REQUEST": 0,
                                             "ASSESSMENT": 0, "OFFER": 0,
                                             "OTHER": 0})
            if r["classification"] in s:
                s[r["classification"]] += 1

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": lookback,
        "messages_scanned": len(msgs),
        "classifications": results,
        "submitted_count": len(submitted),
        "ack_coverage": f"{len(acked)}/{len(submitted)}",
        "missing_acks_24h": missing_acks,
        "lane_outcome_stats": lane_stats,
        "unmatched": [r for r in results if not r["ledger_company"]],
        "live": live,
    }

    if out_dir:
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        path = os.path.join(out_dir, f"inbox-outcomes-{stamp}.json")
        json.dump(report, open(path, "w"), indent=2)
        print(f"report -> {path}")

    if live:
        logged = 0
        for r in results:
            seen.add(r["message_id"])
            if r["classification"] in ("OTHER",) or not r["ledger_company"]:
                continue
            fs = linked_fit_score(r["ledger_company"], ledger_idx)
            details = {
                "outcome": r["classification"],
                "company_key": r["ledger_company"],
                "subject": (r["subject"] or "")[:120],
                "match_how": r["match_how"],
                "backfilled": False,
                "message_date": r["date"],          # mail receipt date
                "message_id": r["message_id"],
                "source": "inbox-listener-live",
                # Body excerpt lets the downstream hooks extract terms /
                # signals from quoted employer text. The hooks treat a
                # missing excerpt as "not stated" — never invented.
                "quoted_text": (r.get("body_text") or "")[:2000],
            }
            if fs is not None:
                details["fit_score"] = fs   # score→conversion measurable
            # Channel attribution for notification tripwires (additive;
            # the dispatcher only reads `outcome`).
            if r.get("indeed"):
                details["channel"] = "indeed-notification"
            elif r.get("linkedin"):
                details["channel"] = "linkedin-notification"
            details = json.dumps(details)
            subprocess.run(
                [sys.executable, LOG_EVENT, "employer_response",
                 "--company", r["ledger_company"],
                 "--details", details],
                capture_output=True, timeout=30,
            )
            logged += 1
        json.dump(sorted(seen), open(seen_path, "w"))
        print(f"telemetry: logged {logged} employer_response events; seen-set={len(seen)}")

    # console summary
    from collections import Counter
    print(f"\nscanned={len(msgs)} submitted={len(submitted)} acked_companies={len(acked)}")
    print("by_class:", dict(Counter(r["classification"] for r in results)))
    print(f"missing_acks(>24h, no auto-ack): {len(missing_acks)}")
    for m in missing_acks[:15]:
        print(f"  ! {m['company']} | {m['title']} | {m['resume_lane']} | {m['date_submitted']}")
    print("lane_stats:", json.dumps(lane_stats, indent=1))
    invites = [r for r in results if r["classification"] == "INTERVIEW_INVITE"
               and r["pipeline_related"]]
    if invites:
        print("PIPELINE INVITES:")
        for r in invites:
            print(f"  * {r['from']} | {r['subject']} | {r['ledger_company']}")
    rejects = [r for r in results if r["classification"] == "REJECTION"
               and r["pipeline_related"]]
    if rejects:
        print("PIPELINE REJECTIONS:")
        for r in rejects[:10]:
            print(f"  x {r['from']} | {r['subject']} | {r['ledger_company']}")
    noise = [r for r in results if r["classification"] == "INTERVIEW_INVITE"
             and not r["pipeline_related"]]
    if noise:
        print(f"non-pipeline interview chatter (excluded): {len(noise)}")
    # Surface LinkedIn notifications explicitly — the channel is
    # unwatched, so these are the only tripwire we have.
    li = [r for r in results if r.get("linkedin")]
    if li:
        print(f"linkedin notifications (unwatched channel — email tripwire "
              f"only): {len(li)}")
        for r in li[:15]:
            print(f"  in {r['from']} | {r['subject']} | "
                  f"{r['classification']} | {r['ledger_company']}")
    # Indeed notifications: same treatment — content lives behind Indeed
    # login, so the tripwire only proves outreach happened. A notification
    # means "check the Indeed inbox"; the classifier labels what the
    # notification subject itself reveals.
    ind = [r for r in results if r.get("indeed")]
    if ind:
        print(f"indeed notifications (unwatched channel — email tripwire "
              f"only, check Indeed inbox): {len(ind)}")
        for r in ind[:15]:
            print(f"  in {r['from']} | {r['subject']} | "
                  f"{r['classification']} | {r['ledger_company']}")


if __name__ == "__main__":
    main(sys.argv[1:])
