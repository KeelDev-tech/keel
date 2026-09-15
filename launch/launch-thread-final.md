# Launch thread — FINAL (awaiting Trent's approval to post)

Status: FINAL copy. Platform TBD at approval time. Nothing published.

---

1/ Job applications are hours of grind per role. The "auto-apply" tools that
promise to fix it mostly became spam cannons: identical applications,
invented qualifications, blasted at everything. It burns candidates and
poisons the channel for everyone.

2/ So I built Keel: an open-core job-application autopilot with a
truthfulness contract. It only ever claims what you tell it is true —
verified facts, a canonical answer bank, banded rules for gray areas, and
hard gates that stop the run when a question can't be answered honestly.

3/ Three rules run the whole system: (1) truthfulness gates — never invents
experience, degrees, or answers; (2) explicit confirmation — nothing counts
as submitted unless the page itself confirms it; (3) fail closed — ambiguity
parks the lead and moves on, never guesses.

4/ Open-core on purpose (Apache-2.0): discovery/scoring, truthful resume
tailoring, the answer bank, prescreen gates, ATS detection + capability
radar, the launch-packet builder, telemetry, dashboard. Private by design:
the submission techniques ATS vendors could fingerprint — publishing those
would get everyone's pipeline blocked.

5/ Proof the discipline works: the private production pipeline verified 53
submitted applications in ~48 hours. The repo itself makes no submission
claims — it's the tools, the gates, and the telemetry behind them.

6/ Looking for: feedback on the honest-automation contract, contributors who
know ATS behavior or want to sharpen answer-bank ergonomics, and seed-stage
conversations with people who care about automation that refuses to lie.
This is Keel — honest automation, end to end. Repo in comments.
