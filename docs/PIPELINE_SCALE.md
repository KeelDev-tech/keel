# Bounded public-source pipeline and reproducible local benchmark

The existing `keel.py discover` and `verify` paths now use bounded normalized
board retention, disk-spooled candidate selection, cooperative CPU deadlines,
strict source field types, and indexed verification commits. These are changes
to the default public-source pipeline, with no new dependency or paid service.

## Discovery and verification behavior

`PublicBoardReader` retains at most 20,000 normalized records and 16 MiB of
canonical record bytes across its LRU board cache. A complete normalized board
must fit within 8 MiB and the existing 20,000-posting ceiling. Whole boards are
evicted; a later read may fetch an evicted board again within the same request
and deadline budgets. These are logical record/encoded-byte limits, not process
RSS guarantees. Raw responses, a board being normalized, Python object overhead,
existing queue documents, and filesystem buffers add transient memory. The
existing public HTTPS transport retains its own 4 MiB response limit.

Discovery writes candidate records to an owner-only temporary file, capped at
40,000 records or 32 MiB, whichever comes first. This spool is closed and removed
on success, failure, or a normal process exit; no raw source bodies are retained.
If the candidate budget is exceeded, the entire intake returns `HELD_CAPACITY`
with zero queue writes. Temporary-write failure returns `HELD_STORAGE`; an
observed HTTP 429 still withholds the entire batch. The tool does not repeatedly
retry or claim that a truncated source has been completely processed. The
underlying temporary filesystem still needs enough available space.

Candidates are replayed in registered-source order and provider-record order
under the existing queue lock. Deduplication checks the **current** queue and
ledger, so a concurrent writer cannot cause stale duplicate decisions or leave
the selected batch unnecessarily underfilled. The first `max_new` currently
eligible unique candidates are added, preserving prior ordering and identity
semantics. A small `max_new` bounds writes, not the work required to completely
read the selected sources. Narrow sources or filters when a run exceeds its
explicit capacity.

The reader validates `isListed` as an actual boolean when present, provider
location containers, bounded location strings and bounded advertised URL types.
An invalid record rejects the complete board; it is not silently skipped.
Ashby `isListed=false` means a posting is available through a direct link but
should not be listed on a job board. Discovery continues to exclude it;
verification of an already supplied exact direct link may still observe the
published posting. This is not proof of form completeness, acceptance, applicant
eligibility or approval. Official semantics:
https://developers.ashbyhq.com/docs/public-job-posting-api

Deadlines are checked after fetch, on cache hits, periodically during board
normalization and candidate processing, after acquiring commit locks and before
queue writes. Queue-lock waits are capped by the remaining run budget. These are
cooperative checks; they do not kill a stuck trusted callback or interrupt an
operating-system write already in progress. Discovery holds before committing
its single queue file when a processing deadline expires. Verification retains
its existing per-file atomicity: a later file can be withheld after earlier files
committed; committed events retain replayable outbox state. Existing outbox
maintenance is separately bounded by its existing lock semantics.

Verification indexes current `(queue path, role ID)` rows once inside the lock,
then uses indexed lookups while retaining duplicate checks, current exact posting
identity checks and full snapshot equality. It does not relax holds or promote
roles to READY.

## Run the actual offline pipeline benchmark

From the extracted source directory, with a new path under an existing parent:

```bash
python3 -B -m keel_next benchmark --home /tmp/keel-new-benchmark
```

The Python API is:

```python
from keel_next.benchmark import run_benchmark
report = run_benchmark('/tmp/keel-another-new-benchmark',
                       boards=3, jobs_per_board=100, max_new=150)
```

The benchmark refuses existing homes. It supports 1–10 boards, 1–200 jobs per
board and at most 1,000 total postings. It is intended for a dedicated process;
it refuses an already active tracemalloc session. It temporarily scopes legacy
queue-lock and telemetry paths to the synthetic home and restores them afterward.

The real shipped implementations initialize the workspace, ingest a bounded
first batch, ingest the remaining batch, replay duplicate discovery, withhold a
whole batch after an injected HTTP 429, verify postings, replay during cooldown,
flush the durable telemetry outbox and calculate supply conservation. Only source
transport is synthetic. Accidental socket construction is rejected within the
benchmark scope. This is not an operating-system sandbox or live provider test.

Thirteen invariant checks determine `BENCHMARK_PASSED` or `BENCHMARK_FAILED`.
The report includes the exact workload and SHA-256, relevant source-file hashes,
Python/platform information, phase durations, real event/queue/fetch counts,
cache/spool high-water values and peak traced Python allocations after imports.
Tracemalloc does not measure all native allocations or process RSS. Timing and
memory are descriptive measurements on this host. Single-run results do not
prove a production speedup or superiority over another system. Source hashes are
local reproducibility evidence, not signatures or dependency attestation.

The benchmark writes `benchmark-report.json` inside its new synthetic workspace.
It invokes no model, network provider or submission handler and grants no
execution authority. `outbox_exactly_once_in_fixture` describes this local
replay experiment; it does not assert exactly-once external side effects.

Regression checks require only the standard library:

```bash
PYTHONPATH=.:engines python3 -B -m unittest discover -s tests -p 'test_pipeline_*.py'
```
