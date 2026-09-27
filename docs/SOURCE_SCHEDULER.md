# Durable public-source scheduling

`engines/source_scheduler.py` integrates the existing `pipeline_service.discover`
with a persistent fair cursor. The original all-source discovery behavior stays
available. Scheduled runs select up to 16 registered enabled boards, allocate a
positive share of the intake allowance to each, and carry one shared request and
deadline budget across the batch. The default is four boards, 200 new leads,
50 requests and 120 seconds. All new rows remain parked for verification.

Call `scheduled_discover(workspace, max_boards=4, max_new=200, timeout=120,
max_requests=50)` from the same host environment as the existing pipeline.
`titles` and `locations` retain the pipeline's public-posting filters. This is
an on-demand controller; it does not install a background daemon or recurring
task. Large paginated boards may need a higher explicit request budget, capped
at 1,000. No paid service or model is required.

Selection prioritizes never-attempted and oldest-attempted eligible sources.
Successful reads wait five minutes; failed reads use bounded exponential backoff.
Unattempted boards retain their previous turn when a shared budget stops a run.
The lock prevents concurrent cooperating controllers. State is bounded to 100
sources and 256 KiB, and malformed state or a regressing clock fails closed.

Each completed source commits independently. A later HTTP 429 immediately stops
further reads, leaves already committed complete-source intake intact, and
persists a minimum five-minute scheduler cooldown. The existing HTTP layer's
host cooldown still applies. A scheduler deadline is cooperative: trusted
injected callbacks must honor their transport budget; this is not OS process
isolation or a distributed scheduler.

An interrupted controller returns `HELD_RECOVERY`. Call
`recover_interrupted(workspace)` to validate and fingerprint current local
queues and ledger, record an unknown previous intake count, and enter a
five-minute recovery cooldown. Later public rereads use the real pipeline's
exact-identity deduplication. Recovery never changes application outcomes,
unknown attempts, approvals or holds. Operator-controlled files and advisory
locks are not a security boundary against arbitrary code under the same OS user.

Run `keel_next.source_scheduler_demo.run_demo(new_home)` for a socket-denied,
64-board experiment. It compares the existing 50-request full-scan hold with
four successful bounded batches, checks duplicate replay, and kills a real
child process after queue commit but before the scheduler checkpoint. The
result reports observed checks and counts; it does not claim production
throughput or competitiveness.
