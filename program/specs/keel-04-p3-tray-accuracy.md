# Keel 0.4 Spec — P3: Tray accuracy — separate genuine missing answers from failed lookups

**Status:** draft for review
**Date:** 2026-09-17
**Grounding rule:** every claim about current behavior was verified by reading
the code cited. Where the code contradicts the handoff brief, the code wins
(discrepancies noted inline).

---

## 1. Problem

The input tray currently charges **four different kinds of blockage** to
Trent as if they were all "questions he must answer":

| Source | Example live today | Current rendering |
|---|---|---|
| Genuine missing answer | "Do you consent to interview recording?" | NEEDS-YOU card — correct |
| **Intel lookup failure** | 38 Stripe leads parked 2026-09-17 17:3x PDT: *"Employer form pattern for Stripe with no form intel in packet to rule it out — needs Trent's explicit input (consent): 'WhatsApp recruiting opt-in'"* | NEEDS-YOU card — **misattributed** |
| Software error | K06 prescreen-exception parks (park-with-reason, never CLEAN) | NEEDS-YOU card — misattributed |
| Stale packet | packet built before materials/intel refreshed | NEEDS-YOU card — misattributed |

The Stripe WhatsApp case is the worked example. The mechanism, verified in
code:

1. Stripe's `application_url` is `stripe.com/careers/apply/…` (ledger row
   `ATS8-GREENHOUSE-STRIPE-ACCOUNT-EXECUTIVE-PRODUCT-PAYOUTS-20260915-J8196259`).
   `form_intel.probe_url()` matches only `job-boards.greenhouse.io`,
   `boards.greenhouse.io`, and `lever.co` URL shapes — stripe.com falls
   through to `_unknown_ats_intel()` (form_intel.py:141-170).
2. Since the K49 port, the unknown-ATS fallback runs passive inspection but
   is **advisory-only**: `questions == []`, `is_authoritative_intel()` False,
   `authoritative_questions()` returns []. Correct — a partial label must
   never park a lead.
3. `prescreen.screen_packet()` sees `not has_form_questions` and, because
   Stripe has harvest-confirmed blocker priors, parks **blind** via
   `_prior_reason(company, emp_key, blocker, blind=True)` (prescreen.py:
   947-957, 1280-1302): *"with no form intel in packet to rule it out —
   needs Trent's explicit input (consent)"*.
4. `genuine_blockers()` (input_tray_digest.py:128) passes it through;
   `classify_card()` (line 211) has no pattern for it; it renders as a
   NEEDS-YOU question.

**The misattribution:** the card says "needs Trent's explicit input", but
the actual missing piece is *form intel* — only a form read can determine
whether Stripe's live form asks the WhatsApp question at all. Trent cannot
answer a question he cannot see. Two sub-questions are conflated:

- (a) **Does this form ask it?** — an intel_gap, answerable only by a
  lookup. Routing it to Trent wastes his taps and buries answerable cards.
- (b) **If asked, what is Trent's call?** — a genuine consent decision,
  answerable only by him, and *only meaningful once (a) is confirmed*.

**Discrepancy vs the handoff brief:** the brief framed the reclassification
as moving the Stripe pattern wholesale out of the tray. That would be wrong
for one class of blockers (see §3.3): the digest layer already deliberately
keeps no-AI pledges, recording consent, essays, and D1 judgments as
NEEDS-YOU even when blind (input_tray_digest.py:176-180, "Deliberately NOT
reclassified"). The spec preserves that carve-out and explains why consent/
preference priors differ from integrity priors.

## 2. Goals

1. Every tray card carries a **source label**: `genuine_missing`,
   `intel_gap`, `software_error`, or `stale_packet`.
2. `intel_gap`, `software_error`, and `stale_packet` cards **never render
   as NEEDS-YOU questions**. They surface once in the digest's system
   section (existing pattern), then stay quiet — with a named owner and a
   retry/resolution path, not a question mark.
3. When an `intel_gap` resolves (form intel arrives and confirms the
   question), the card **promotes** to `genuine_missing` with the confirmed
   question text — no invented answers, no blind banking.
4. Employer-specific consent grouping (`family_of` / `card_key_for`) is
   preserved; the bank-provenance audit rule (card_key must have a
   tray-answers record) is unchanged.

## 3. Design

### 3.1 Blocker-source taxonomy (exact classification rules)

Classification happens in `input_tray_digest.classify_card()` — **digest
layer only**, pure function, pinned by regression tests. Queues are never
mutated to clean the digest (existing convention from
PERMANENTLY_BLOCKED_PATTERNS).

**`genuine_missing`** — the question is confirmed to exist (authoritative
form intel, verified posting text, or Trent-visible form state) AND the
answer is Trent's own words/decision. The 14 `tray_triage.py` CLASSES are
the closed vocabulary: `d1_travel`, `d1_office`, `schedule_commitment`,
`no_ai`, `recording_consent`, `attestation`, `essay`, `consent`,
`personal_fact`, `compensation`, `sponsorship`, `employment_fact`,
`availability`, `your_call`.

**`intel_gap`** — the blocker depends on a fact only a lookup can provide.
Exact rules (any match → intel_gap):

- Park reason matches `Employer form pattern for .* with no form intel in
  packet to rule it out` (the `blind=True` template, prescreen.py:950-955)
  AND the `_blocker_class` hint is a **preference/consent** class
  (`consent`, and only those) — see §3.3 for why integrity classes are
  excluded.
- `form_intel` payload is advisory-only (`is_authoritative_intel()` False)
  for a role whose employer has priors — the K49 `passive_intel` case.
- Verification-class: `verify_retry` verdict `ambiguous` with a
  cooldown/transport message (not a dead/live verdict).

Routing: to the **intel pipeline**, never to Trent. Concretely: the card
renders in the digest system section as
`SYSTEM-BLOCKED (intel-gap): <employer> form unreadable — <prior> cannot be
confirmed or ruled out; queued for authoritative form probe`. The probe
retry goes through the existing paths (`form_intel.probe_url` authoritative
branches; browser-path read where HTTP extraction is structurally absent).
Retry is bounded and watermarked; after N failed attempts the card notes
"intel exhausted — awaiting Trent's call on the *unverified* prior" and
only then may it surface as a question, framed honestly as unverified.

**`software_error`** — exception paths: K06 prescreen-exception parks
(`prescreen.park_lead` with an exception reason), FRP module failures,
queue-write failures. Routing: engineering (Program Office register),
surfaced as `SYSTEM-BLOCKED (engineering)`. Never a question.

**`stale_packet`** — packet `built_ts` older than the materials/intel
refresh watermark for the role. Routing: rebuild queue (feeder), surfaced
as `SYSTEM-BLOCKED (stale-packet)`. Never a question.

### 3.2 The promotion path (intel_gap → genuine_missing)

When authoritative intel later arrives for a role with an open intel_gap
card:

1. `authoritative_questions(intel)` is non-empty → the prior is checked
   against confirmed questions.
2. Prior **confirmed** → new `genuine_missing` card with the confirmed
   question text, same family grouping (so the 38 Stripe leads collapse to
   one card). The intel_gap card is marked resolved in the digest JSON.
3. Prior **ruled out** → the park reason is voided at the prescreen layer
   (the lead re-flows through `screen_packet`; no tray action needed).
4. Intel still absent after the retry budget → the card stays intel_gap;
   it never degrades into a blind question.

Banking rule (extends the existing `BLIND_SAFE_BANKED_KEYS` precedent,
prescreen.py:1035): a Trent answer is applied to an employer-prior blocker
only when the question is **confirmed** (authoritative intel) or the key is
explicitly blind-safe. The 2026-09-17 voiding of the unprovenanced
`stripe_whatsapp_optin=YES` stays void — provenance first, always.

### 3.3 Why integrity priors stay NEEDS-YOU when blind

The existing digest comment (input_tray_digest.py:176-180) is load-bearing:
no-AI/unaided-work pledges, recording consent, essays, and D1 judgments
stay NEEDS-YOU even when the prior is blind. Rationale, now made explicit:

- For **integrity-class** blockers, Trent's decision is needed *regardless
  of exact wording* — even a reworded no-AI pledge on the live form needs
  his call, and the safe default (park) is the product. Hiding the card
  would trade his visibility for lead velocity, violating the standing
  never-pre-authorize rule.
- For **preference-class** blockers (WhatsApp opt-in, marketing comms), a
  blind question is unanswerable *in context* — his eventual Yes/No only
  binds to a confirmed question, and asking blind burns his scarcest
  resource (taps) on something a lookup should resolve first.

The classifier encodes this as: `blind=True` + `_blocker_class` in
`{no_ai, recording_consent, attestation, essay}` → stays NEEDS-YOU;
`blind=True` + class in `{consent}` → intel_gap. `d1_travel`/`d1_office`
and `personal_fact`/`compensation`/`sponsorship` keep their existing
paths (they are posting-text or Trent-fact questions, not prior-driven).

### 3.4 Worked example: the Stripe WhatsApp pattern

Today: 38 leads carry *"Employer form pattern for Stripe with no form
intel in packet to rule it out — needs Trent's explicit input (consent):
'WhatsApp recruiting opt-in'"* as NEEDS-YOU cards (family-grouped).

After this spec:

1. Digest renders **one** system card:
   `SYSTEM-BLOCKED (intel-gap): Stripe form unreadable via HTTP
   (stripe.com/careers/apply/* has no ATS probe branch) — 'WhatsApp
   recruiting opt-in' prior cannot be confirmed or ruled out. 38 leads
   held. Queued for authoritative form read.`
2. The intel pipeline attempts an authoritative read (browser-path probe;
   K49 passive labels are advisory-only and explicitly cannot confirm).
3. If confirmed → one `genuine_missing` card, triage class `consent`
   ("a consent choice — your preference, no verified default"), 38 leads
   unblocked by one tap, banked with his-words provenance.
4. If ruled out → 38 parks voided, leads re-flow.
5. Trent's tap budget is spent only on step 3, never on step 1.

## 4. Test plan

| Test | What it proves |
|---|---|
| `test_blind_consent_prior_is_intel_gap` | the exact Stripe WhatsApp reason → `intel_gap`, never NEEDS-YOU |
| `test_blind_noai_prior_stays_needs_you` | blind no-AI prior → still NEEDS-YOU (integrity carve-out) |
| `test_k06_park_is_software_error` | prescreen-exception park reason → `software_error` |
| `test_advisory_intel_never_gates_tray` | K49 `passive_intel` payload → intel_gap, not a question; `authoritative_questions` empty |
| `test_intel_gap_promotes_on_confirm` | authoritative intel confirming the prior → `genuine_missing` card with confirmed text; family grouping preserved |
| `test_intel_gap_void_on_ruleout` | prior ruled out → park voided, no card |
| `test_no_blind_banking` | unconfirmed prior + banked consent key → not applied (provenance rule) |
| `test_digest_layer_purity` | classification changes nothing in queue files (queues byte-identical before/after digest run) |
| `test_employer_consent_grouping` | 38 Stripe leads → one card key; employer scoping intact |

## 5. Acceptance criteria

- [ ] Every card in `hidden_files/input-tray.json` carries `source` in
      `{genuine_missing, intel_gap, software_error, stale_packet}`.
- [ ] Zero `intel_gap`/`software_error`/`stale_packet` cards render in the
      NEEDS-YOU section (digest test on the live 2026-09-17 Stripe cohort).
- [ ] The 38 Stripe WhatsApp parks collapse to one intel_gap system card
      with a named retry path.
- [ ] Promotion path tested end-to-end: confirm → genuine_missing card;
      rule-out → voided park.
- [ ] No-AI, recording-consent, essay, and D1 cards are unchanged in
      placement and volume (no visibility regression on integrity classes).
- [ ] `tray_answer.py` still resolves only `genuine_missing` cards;
      answering an intel_gap card is refused (fail closed).

## 6. Non-goals

- **No weakening of fail-closed parks.** The prescreen still parks blind
  priors; this spec changes only how the *digest* labels and routes the
  resulting cards. `screen_packet` verdicts are untouched.
- No change to the bank-provenance audit rule or the never-pre-authorize
  list (no-AI, residence, essays, recording consent).
- No new ATS probe transports — the spec routes to *existing* probe paths
  and bounds retries; building a stripe.com extractor is separate work.
- No reclassification of `PERMANENTLY_BLOCKED_PATTERNS` entries.

## 7. Risks

- **Integrity-class over-rotation.** If the `_blocker_class` hint
  mislabels a novel integrity blocker as `consent`, it would sink to
  intel_gap and hide from Trent. Mitigation: the hint list is closed and
  default-closed — unknown classes stay NEEDS-YOU; only the enumerated
  preference classes route to intel_gap.
- **Intel retry budget as a new park.** A form that is permanently
  unreadable could hold leads in intel_gap forever. Mitigation: bounded
  retries, then honest surfacing as an *unverified-prior* question —
  never silent.
- **Digest/queue skew.** Classification is render-time; a lead whose park
  clears between digest runs may show a stale card. The existing
  watermark/fresh-keys machinery already handles this; the spec adds no
  new staleness class.
- **Trent's mental model.** "Fewer questions" must not read as "the tray
  is ignoring Stripe." The system card names the 38 held leads and the
  retry path explicitly.
