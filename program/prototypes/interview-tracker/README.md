# interview-tracker prototype (G-4 / B-26 + G-7 / PP-12)

**Status: PROTOTYPE — not wired to production. No live ledger, no mailbox
connector, no real outcome ingestion. G1 privacy-counsel sign-off gates all
real outcome data handling before anything here touches real data.**

## What it proves (7/7 tests green)

- **Denominator integrity:** a 500-submission generated replay reconciles 1:1
  with the ledger — no missing, no double-counted, state counts sum to 500.
- **No inference:** a fabricated "congratulations!" signal with an unlawful
  evidence kind, or a vendor claim, is rejected with `ValueError` — the state
  never advances.
- **Fail-closed authorization:** mailbox signals are disabled until the user
  authorizes the connection; unauthorized sources raise; the same signal
  works after authorization.
- **First-class unknowns:** `expire_unknown` marks stale SUBMITTED rows as
  `EXPIRED_UNKNOWN` and returns the cohort with its denominator; terminal
  states are untouched; unknowns are never counted as successes.
- **Honest funnel:** `funnel_view()` (applications → responses → interviews →
  offers) derives only from tracker + cohort data; every figure carries a
  count, a denominator, and a provenance label; `expired_unknown` ships with
  an explicit "never counted as successes" note.
- Terminal states can only be corrected via `cohort_reconciliation`
  (honest, fully logged), and `reconcile()` surfaces missing/extra IDs.

Run: `python3 test_interview_tracker.py`

## What the departments must do to productize it

- **Build (B-26):** adapt `Tracker` to the live telemetry schema and ledger
  (this prototype registers submissions from an injected ledger list); build
  the tracker surface UI with per-state provenance display; keep the
  EXPIRED_UNKNOWN path wired to the cohort job, not to user taps.
- **Pipeline Performance (PP-12):** own the funnel surface mapping
  (telemetry → what the user sees), the cohort job that calls
  `expire_unknown` at the 30/60-day windows, and the reconciliation cadence
  against the ledger; hold every published figure to the provenance rule.
- **Competitive Intel (CI-04):** the interview-rate cohort design — consent
  model, independent relevance labels, job-age checks, uncertainty bands —
  consumes this tracker's states; CI-04 defines what counts as a mature
  cohort, this prototype only carries the states.
- **Authorization flow (Build):** the mailbox connector's consent UX and the
  authorization registry; this prototype models it as
  `authorize_source("mailbox")` and nothing may shortcut that call.
- **G1 gate:** privacy-counsel sign-off on outcome-ingestion data handling
  before ANY real signal source is authorized — the fail-closed default is
  not a bypassable step.
