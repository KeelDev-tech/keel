# Comment bank — DRAFT ONLY. Never post anything without the founder's explicit go-ahead.

Repo: https://github.com/KeelDev-tech/keel (KeelDev-tech/keel, public, Apache-2.0)

Voice: the founder's hand — first person, terse, direct, grounded. No defending, no hype,
no invented numbers. When the sources don't answer, say so.

---

## FIRST COMMENT (post within 60 seconds of the Show HN submission)

Repo: https://github.com/KeelDev-tech/keel — Apache-2.0, open-core.

The contract, stated plainly: Keel only ever claims what you tell it is true.
Verified facts, a canonical answer bank, banded rules for gray areas, and hard
gates that stop the run cold when a question can't be answered honestly. It
never invents experience, degrees, or answers. Nothing counts as submitted
unless the page itself confirms it. Ambiguity parks the lead and the run
moves on — fail closed, never guesses.

Three things I'd genuinely value from HN:

1. The honest-automation contract — what's missing from it?
2. The open-core boundary — did we draw the line in the right place?
3. Contributors — especially around ATS behavior, answer-bank ergonomics,
   and outcome analytics.

Happy to answer anything. I won't defend the parts that are weak; I'll just
say so.

---

## RESPONSES

### 1. "Is this another spam cannon?"

**Objection restated:** Auto-apply tools have mostly become spam cannons —
identical applications with invented qualifications blasted at scale. Why
isn't Keel the same thing with a friendlier name?

**Answer:** That's exactly the thing it's built against. The truthfulness
contract is the product, not a marketing line: Keel only claims what you tell
it is true — verified facts, a canonical answer bank, banded rules for gray
areas, and hard gates that stop the run when a question can't be answered
honestly. It never invents experience, degrees, or answers. And in the public
repo the loop stops at the launch packet — the README's "What it does NOT do"
section is explicit: Keel never submits an application, never invents
qualifications, makes no submission claims. Ambiguity parks the lead and it
moves on. If it can't be honest, it doesn't run. A spam cannon optimizes for
volume; this optimizes for every claim being true.

**Honest boundary:** The public repo makes no submission claims at all, so
"it won't spam" is a property of the public loop's design (it stops at the
packet). What any individual does with a private execution layer of their own
is outside what the repo can control — the code it ships can't be a spam
cannon.

### 2. "Why open-core instead of fully open source?"

**Objection restated:** If this is really about honest automation, why isn't
all of it open?

**Answer:** The split is defensive, not commercial. The private half holds
submission-behavior techniques ATS vendors could fingerprint — form-event
sequencing, per-platform commit techniques, CAPTCHA-handling specifics,
credential/verification-code flows, the live API-direct transport. SPLIT.md's
rule of thumb: if publishing a method would help a vendor block automated
applications, it's private; if it helps an applicant run an honest,
verifiable, fail-closed pipeline, it's public. Publishing the private half
would get the pipeline blocked for everyone running it — including everyone
who clones the repo. The public half is Apache-2.0 and it's the parts that
matter for the contract: discovery/scoring, truthful resume tailoring, the
answer bank, prescreen gates, ATS detection plus the capability radar, the
launch-packet builder, telemetry, and the dashboard.

**Honest boundary:** I'm deliberately not publishing the anti-fingerprinting
techniques themselves — that's the whole point of the split. You're trusting
that the boundary is drawn where I say it is, and I respect that that's an
ask. The contract rules that keep the system honest are all inspectable; only
the fingerprintable mechanics are withheld.

### 3. "Where's the proof? / How do I verify the 55 submissions claim?"

**Objection restated:** You claim 55 verified submissions
from a private pipeline nobody can see. How is anyone supposed to check that?

**Answer:** Fair question, and I'll state the limits plainly. The figure is
the SUBMITTED row count in my private production pipeline's ledger at launch
— 55 submissions, each counted only on explicit page-confirmation evidence.
You can't independently re-run that pipeline; the public repo
isn't it, and the repo itself makes no submission claims. What's verifiable
is the discipline: the explicit-confirmation rule (nothing counts as
submitted unless the page itself confirms it; the ledger increments only on
real confirmation evidence) and the append-only telemetry with fail-closed
reporting rules (rates with denominators under 5 read "insufficient outcome
data"; hypotheses are hedged and labeled) are all public code. The 55 is a
traction figure for the discipline, not a claim the repo asks you to take on
faith about the code. Don't trust the number — inspect the gates and tell me
what's missing.

**Honest boundary:** We don't have this yet: an independently auditable
record of the private pipeline's submissions. The discipline that produced
the number is public and inspectable; the number itself is my claim, stated
as such. If that's not enough, that's a reasonable position — the repo stands
on its own as tools, gates, and telemetry.

### 4. "How do you avoid ATS fingerprinting?"

**Objection restated:** ATS vendors actively fingerprint automation. How does
Keel avoid that?

**Answer:** The anti-fingerprinting strategy is the open-core boundary
itself. Nothing in the public repo helps an ATS vendor identify or block
automated applications — the README states "No fingerprinting surface" as
contract rule 4. The public ATS detection rules (URL patterns, board APIs)
are read-only identification only: detection, no submission behavior. The
submission paths those probes find are exercised exclusively in the private
layer. I'm not publishing how the private layer specifically avoids
fingerprinting, because the techniques staying unpublished is precisely what
keeps them working.

**Honest boundary:** We don't have this yet: any public technical account of
the anti-fingerprinting mechanics. If I published specifics, vendors could
study and block them — so the detailed answer is intentionally withheld,
and I won't give a partial one either.

### 5. "What's the business model?"

**Objection restated:** How does this make money?

**Answer:** What I've committed to in the open: the public half is
Apache-2.0, and the README names "managed execution is the hosted tier" as
the private-layer commercial shape. Beyond that I'm talking to people who
care about automation that refuses to lie — seed-stage conversations, no more
specific than that.

**Honest boundary:** We don't have this yet: pricing, hosted-tier
availability, revenue targets, or fundraising status. None of that is in the
files or decided. Happy to say "I don't know yet" instead of inventing a
plan.

### 6. "Why should I trust this if the executor is private?"

**Objection restated:** The part that actually submits applications is
hidden. Why should anyone trust it?

**Answer:** Two reasons, both inspectable. First, the honest-automation
contract is fully public — truthfulness gates, explicit confirmation, fail
closed, no fingerprinting surface. The EXECUTOR CONTRACT documented at the
end of the public apply loop specifies exactly what any submission layer must
do, so a private executor that violated it would be detectable against the
spec. Second, the per-field verification protocol in the generic brief
(verify after every field, blur test, dropdown re-open check) is public,
because it's a correctness practice, not a fingerprintable technique. So the
honesty-critical parts are inspectable — what's private is only the
DOM-event-level sequences vendors could fingerprint, and keeping those
private protects everyone running the pipeline.

**Honest boundary:** This is a "trust the boundary" ask, and I know it. The
sources don't give you a way to audit the private executor's internals — that's
the deliberate trade. What they give you is the public spec it must satisfy
and the public contract it must obey.

### 7. "Why should I believe the number isn't inflated?"

**Objection restated:** Metrics get inflated. Why should anyone take the 55
at face value?

**Answer:** The reporting rules are designed against exactly that. The ledger
increments only on explicit confirmation evidence — browser-task acceptance
doesn't count as submission, only the page itself confirming does. Outcome
analytics are computed with fail-closed rules: any rate with a denominator
under 5 reads "insufficient outcome data," hypotheses are hedged and labeled.
The telemetry is append-only. These are standing rules in the code, not
one-off discipline.

**Honest boundary:** The number is still my claim about my private pipeline,
and the rules above are what I built to keep myself honest — I'm not going
to pretend they eliminate the trust gap. What's auditable is that the rules
exist in the public code; whether they were followed on the private run is
something you'll have to take my word for. I won't oversell it.

---

## Notes for the founder

- All 7 FAQ objections covered, each with objection restated + grounded
  answer + honest boundary.
- Honest boundaries were marked on 6 of 7 (objections 2–7). Objection 1
  ("spam cannon") was fully answerable from the files; its caveat is a
  design fact, not a gap.
- OPEN items from launch-faq.md that could surface as follow-ups: business
  model details, private-pipeline scale/stack/response rates, Product Hunt
  in/out, sanitized launch-packet screenshot as demo material
  (docs/assets/dashboard-screenshot.png exists — needs your launch-ready call).
- Do not post anything from this file until you give the go-ahead.
