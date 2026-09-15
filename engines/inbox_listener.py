#!/usr/bin/env python3
"""Inbox outcome listener — classifies ATS/employer mail and links it to ledger entries.

Backfills employer responses (auto-acks, rejections, interview invites, info
requests) against the application ledger. Read-only by default; --live appends
`employer_response` telemetry events via log_event.py (append-only, safe).

Usage:
    python3 inbox_listener.py [--lookback-days N] [--live] [--out DIR]

Classification is rule-based and fully auditable: every label links to the
source message. Spot-check the report before trusting precision.
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
    # keyword sweep for anything the sender filter missed
    kw = [m for m in mail_source.triage(lookback_days=lookback)
            if re.search(r"thank you for applying|your application|interview",
                         f"{m.get('subject','')} {m.get('body_text','')}", re.I)]
    seen = {m["id"] for m in msgs}
    for m in kw:
        if m["id"] not in seen:
            msgs.append(m)
            seen.add(m["id"])

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
                                             "ASSESSMENT": 0, "OTHER": 0})
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
            details = json.dumps({
                "outcome": r["classification"],
                "company_key": r["ledger_company"],
                "subject": (r["subject"] or "")[:120],
                "match_how": r["match_how"],
                "backfilled": True,
            })
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


if __name__ == "__main__":
    main(sys.argv[1:])
