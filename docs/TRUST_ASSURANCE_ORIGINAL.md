# Assurance boundaries

This is a review candidate for the supplied public preparation edition. Its local tests are evidence for specific properties, not a guarantee that every gap has been found. There was no independent security audit, real application submission, live-provider acceptance test, multi-user deployment, or hiring-outcome study in this run.

| Boundary | Implemented behavior | What it does not establish |
|---|---|---|
| External actions | Supported CLI only prepares files; GET/HEAD transport; packets never grant execution | No browser/API submission implementation or authorization issuer |
| Outcomes | Submitted events become UNVERIFIED claims; dashboard never turns quotes into provider proof | Actual application arrival requires a separate trusted provider adapter |
| Applicant values | Explicit source/scope/time/value-bound assertions; defaults are empty; expired/changed values fail | Applicant identity, factual accuracy, authenticated consent, or consent to every employer wording |
| Documents | Workspace containment, size bounds, source hashes, copied bytes, validation at reuse | A file hash does not validate resume claims, remove malicious document content, or authorize upload |
| Network | Public HTTPS only; credentials/method/header restrictions; all DNS addresses checked; IP-pinned TLS; redirects rechecked; bounded bodies, redirects and DNS worker count | This is not an OS egress firewall. Arbitrary plugins, same-user Python and external browser tools are outside it |
| Network time | Per-call timeout and connection shutdown timer; lock and DNS waits use the remaining caller budget (DNS also has a five-second ceiling), with at most 8 lingering resolver workers | A system resolver cannot be forcibly cancelled. Filesystem operations and kernel scheduling are not hard realtime guarantees |
| Local state | Private modes for new core files, durable atomic replacement, advisory locks, preparation leases, explicit telemetry replay IDs | No global transaction across every legacy queue/ledger/learning writer; no hostile-local-user or multi-tenant boundary; no authenticated append-only history |
| Roles | CLI actions are restricted by selected role; eight responsibilities have acceptance evidence | A caller can choose another role or edit code. Roles are neither authentication nor a sandbox; no independent agents are implied |
| Learning | Shadow proposals and explicit missing-meter coverage; ambiguous employer responses stay unlinked | No automatic policy amendments, causal improvement claim, complete unit economics, or differential privacy guarantee |
| UI | Static offline snapshot, escaped data, explicit unknowns, semantic tables and focus styling | Browser/assistive-technology checks were blocked here; full accessibility conformity is unproved |

## Supported surface

`keel.py`, the versioned preparation contract, the checked HTTP transport, core evidence reporting, and the documented shadow CLI are the candidate's supported workflow. Older discovery, reconciliation, analytics, scheduling and worker modules remain available for review and adaptation. Their presence does not mean every historical private orchestration contract has been ported or tested end to end. Raw legacy write sites are inventoried in `audit/source-inventory.json`.

No scheduler was installed and no historical production state was migrated. Do not point this candidate at your production workspace before reviewing the migration and the applicable original register items. Use a copy or an empty workspace first.

## Requirements for a future free execution adapter

A local browser implementation is technically possible without paying another service, but it is a separate, unimplemented authority boundary. It must bind approval to the exact target, form, answers and bytes; record durable intent before the external effect; keep ambiguous outcomes on hold; validate provider-specific receipts; and reconcile history before any retry or transport switch. CAPTCHA, SMS codes, employer credentials, no-AI declarations and legal commitments are not solved by coding around them. No paid fallback is necessary or configured here.

## Costs and superiority

No paid services were used for implementation or are required by the core. Local compute, storage, network access and human review still consume resources. No claim of zero total cost, fully autonomous operation, or state-of-the-art comparative superiority is supported by these tests.

HTTP cooldowns now persist across cooperating workers in one workspace. Same-host reads serialize. Wall-clock changes, unpersisted holds after storage failure, other workspaces and cross-machine/provider budgets remain boundaries; see `docs/HTTP_ADMISSION.md`.
