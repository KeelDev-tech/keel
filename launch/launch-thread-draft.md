# Launch thread draft (6 posts)

Status: DRAFT ONLY — nothing published. Platform TBD at approval time. No
hashtag spam; keep plain text, minimal tags if any.

---

1/ Job applications are hours of grind per role — and the "auto-apply" tools
that promise to fix it are mostly spam cannons: identical applications,
invented qualifications, sprayed at everything. It burns candidates and
poisons the channel for everyone.

2/ So I built Keel: an open-core job-application autopilot with a
truthfulness contract. It only ever claims what you tell it is true — your
verified facts, a canonical answer bank, banded rules for gray areas, and
hard gates that stop the run when a question can't be answered honestly.

3/ Three rules run the whole system: (1) truthfulness gates — never invents
experience, degrees, or answers; (2) explicit confirmation — nothing counts
as submitted unless the page itself confirms it; (3) fail closed —
ambiguity parks the lead and moves on, never guesses.

4/ It's open-core on purpose. The public repo (Apache-2.0) has
discovery/scoring, truthful resume tailoring, the answer bank, prescreen
gates, ATS detection + capability radar, the launch-packet builder, and
telemetry. What stays private: the submission techniques ATS vendors could
fingerprint — publishing those would get everyone's pipeline blocked.

5/ The private pipeline holds 94 verified submissions (as of 2026-09-15).
That's proof the discipline works. The repo itself makes no submission
claims — it's the tools, the gates, and the telemetry behind them.

6/ Looking for: feedback on the honest-automation contract, contributors
who know ATS behavior or want to sharpen answer-bank ergonomics, and early
seed-stage conversations. Repo link in comments.
