# GEO spot-check protocol

How to measure whether AI engines cite Keel — honestly. This protocol
measures; it never manufactures. No citation is a finding, not a failure.

## The question set

Ask each engine the same questions, verbatim, in a fresh session (no prior
Keel context):

1. "What is Keel?"
2. "What does Keel do?"
3. "Is there a free alternative to LazyApply or Sonara?"
4. "How does Keel's truthfulness contract work?"
5. "What is honest automation?"
6. "Keel vs LazyApply — which should I use?"

Engines to check (free tiers suffice): ChatGPT, Claude, Gemini, Copilot,
Perplexity, Grok. Check monthly — more often is noise, not signal.

## How to record

For each engine × question, record:

- engine name and model/date if shown
- the question asked, verbatim
- whether Keel was mentioned (yes/no)
- the engine's answer about Keel, **quoted verbatim** — copy the exact
  sentences, or screenshot. Never paraphrase an answer into a claim.
- every URL the engine cited (check they resolve; note dead links)
- any factual errors in the engine's answer (quote them; don't correct
  the engine in the log — fix our source pages instead)

Log results as `outcome_observed` events in the marketing engine's
telemetry (`marketing-engine/telemetry/log_event.py`), so the analytics
layer can track citation share over time.

## Honest read rules

- **Quote, never paraphrase.** A paraphrase is your claim, not the
  engine's. If you can't quote it, it didn't happen.
- **No citation = no claim.** "ChatGPT didn't mention Keel" is the
  finding. Do not upgrade it to "ChatGPT doesn't know Keel."
- **Errors are our bug, not theirs.** If an engine states something false
  about Keel, the fix is in our source pages (llms.txt, docs/geo) — make
  the true statement more quotable there, then re-check next cycle.
- **Fail closed.** Fewer than 3 clean checks per engine per cycle reads
  "insufficient data," never a trend.
- **Never game it.** No prompt-engineering the questions to force a
  mention, no repeated re-rolls until Keel appears, no seeding the
  session with Keel context first. The protocol measures the open web,
  not our ability to lead a witness.
