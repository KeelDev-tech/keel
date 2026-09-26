"""Synthetic posting identity, durable projection and caller regressions."""
import json
import os
from pathlib import Path
import sqlite3

import pytest

from dedupe_gate import check_candidate, filter_batch, canonical_url
from dedupe_index import DedupeIndex, Source, get_index, keys_for_urls, stage_verdict


def row(url, **extra):
    return {"role_id": "role-1", "company": "Source", "title": "Operations Manager",
            "posting_url": url, "status": "READY", **extra}


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows))
    return str(path)


def index(tmp_path, rows=None, **kw):
    path = write(tmp_path / "queue.json", rows or [])
    idx = DedupeIndex(tmp_path / "index.sqlite3", [Source("west", path)], **kw)
    idx.refresh()
    return idx


def test_source_sourcegraph_same_title_never_hard_duplicate(tmp_path):
    candidate = "https://jobs.lever.co/sourcegraph/def"
    existing = row("https://jobs.lever.co/source/abc")
    idx = index(tmp_path, [existing])
    assert idx.check_candidate("Sourcegraph", existing["title"], candidate)[0] == "suspect"
    assert check_candidate("Sourcegraph", existing["title"], candidate,
                           ledger_rows=[], queue_entries=[existing])[0] == "suspect"


def test_same_company_title_distinct_exact_ids_survive(tmp_path):
    idx = index(tmp_path, [row("https://boards.greenhouse.io/acme/jobs/101")])
    assert idx.check_candidate("Source", "Operations Manager", "https://boards.greenhouse.io/acme/jobs/102")[0] == "suspect"


@pytest.mark.parametrize("left,right", [
    ("https://jobs.lever.co/acme/abc", "https://jobs.lever.co/acme/abc/apply?utm_source=mail"),
    ("https://boards.greenhouse.io/acme/jobs/101", "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=101"),
    ("https://jobs.ashbyhq.com/acme/abc", "https://jobs.ashbyhq.com/acme/abc/apply"),
    ("https://example.com/job?id=123", "https://EXAMPLE.com:443/job?id=123&utm_source=x"),
])
def test_provider_and_url_identity_forms_match(tmp_path, left, right):
    idx = index(tmp_path, [row(left)])
    verdict, evidence = idx.check_candidate("different", "different", right)
    assert verdict == "duplicate"
    assert evidence["source_id"] == "west"
    assert evidence["execution_authorized"] is False


@pytest.mark.parametrize("left,right", [
    ("https://example.com/job?job_id=1", "https://example.com/job?job_id=2"),
    ("https://example.com/job?cid=1", "https://example.com/job?cid=2"),
    ("https://example.com/job?ref=A", "https://example.com/job?ref=B"),
    ("https://jobs.lever.co/acme/ABC", "https://jobs.lever.co/acme/abc"),
    ("https://example.com/Job/ABC", "https://example.com/job/abc"),
    ("https://jobs.lever.co/acme/abc", "https://jobs.eu.lever.co/acme/abc"),
    ("https://example.com/jobs;a=1", "https://example.com/jobs;a=2"),
    ("https://example.com/job?job=1&job=2", "https://example.com/job?job=2&job=1"),
    ("https://example.com:0/job/one", "https://example.com/job/one"),
    ("https://example.com/careers/view#/jobs/one", "https://example.com/careers/view#/jobs/two"),
])
def test_distinct_identity_keeps_case_region_and_query(tmp_path, left, right):
    idx = index(tmp_path, [row(left)])
    assert idx.check_candidate("unique", "unique", right)[0] == "fresh"


def test_multiple_exact_ids_are_ambiguous_in_either_side(tmp_path):
    one, two = "https://jobs.lever.co/acme/one", "https://jobs.lever.co/acme/two"
    idx = index(tmp_path, [row(one)])
    assert idx.check_candidate("other", "other", urls={"posting_url": one, "ats_url": two})[1]["kind"] == "ambiguous_candidate_identity"
    write(tmp_path / "queue.json", [row(one, ats_url=two)])
    idx.refresh()
    assert idx.check_candidate("other", "other", one)[0] != "duplicate"


def test_shared_unknown_application_url_cannot_override_distinct_provider_ids(tmp_path):
    shared = "https://example.com/careers/application123"
    idx = index(tmp_path, [row("https://jobs.lever.co/acme/one", application_url=shared)])
    assert idx.check_candidate("other", "other", urls={"posting_url": "https://jobs.lever.co/acme/two", "application_url": shared})[0] == "fresh"


def test_distinct_unknown_postings_sharing_application_endpoint_survive(tmp_path):
    shared = "https://careers.example/forms/start"
    idx = index(tmp_path, [row("https://careers.example/job/one", application_url=shared)])
    candidate = row("https://careers.example/job/two", application_url=shared)
    assert idx.check_candidate("other", "other", urls=candidate)[0] == "fresh"
    assert check_candidate("other", "other", "", urls=candidate,
                           ledger_rows=[], queue_entries=[row("https://careers.example/job/one", application_url=shared)])[0] == "fresh"


def test_conflicting_unknown_urls_without_posting_field_are_advisory(tmp_path):
    idx = index(tmp_path, [row("https://careers.example/form/one")])
    candidate = {"ats_url": "https://careers.example/form/two", "application_url": "https://careers.example/form/one"}
    assert idx.check_candidate("other", "other", urls=candidate)[0] == "suspect"


def test_all_secondary_candidate_fields_reach_index_and_gate(tmp_path):
    exact = "https://jobs.lever.co/acme/one"
    idx = index(tmp_path, [row(exact)])
    candidate = row("https://other.example.com/jobs/other", ats_url="https://other.example.com/form/one", application_url=exact)
    fresh, duplicates = filter_batch([candidate], index=idx)
    assert not fresh
    assert len(duplicates) == 1


@pytest.mark.parametrize("url", ["https://company.example/", "https://company.example/careers", "https://company.example/confirmation", "https://company.example/form/thank-you"])
def test_shared_generic_destinations_never_posting_proof(tmp_path, url):
    idx = index(tmp_path, [row(url)])
    assert idx.check_candidate("other", "other", url)[0] != "duplicate"


def test_confirmation_is_not_posting_identity_without_exact_provider_id(tmp_path):
    idx = index(tmp_path, [row("https://example.com/job/one", confirmation_url="https://example.com/success/receipt")])
    assert idx.check_candidate("other", "other", "https://example.com/success/receipt")[0] != "duplicate"
    write(tmp_path / "queue.json", [row("", confirmation_url="https://jobs.lever.co/acme/one")])
    idx.refresh()
    assert idx.check_candidate("other", "other", "https://jobs.lever.co/acme/one")[0] == "duplicate"


def test_all_explicit_sources_and_no_backup_auto_discovery(tmp_path):
    write(tmp_path / "data/application-ledger.json", [])
    write(tmp_path / "data/queues/standard-queue.json", [])
    url = "https://jobs.lever.co/acme/special"
    second = write(tmp_path / "data/queues/operator-approved.json", [row(url)])
    backup = "https://jobs.lever.co/acme/backup"
    write(tmp_path / "data/queues/_backup-copy.json", [row(backup)])
    write(tmp_path / "config/identity-sources.json", {"sources": [
        {"source_id": "approved-west", "path": second, "kind": "queue"},
        {"source_id": "standard", "path": str(tmp_path / "data/queues/standard-queue.json"), "kind": "queue"},
    ]})
    idx = get_index(tmp_path)
    assert idx.check_candidate("other", "other", url)[1]["source_id"] == "approved-west"
    assert idx.check_candidate("other", "other", backup)[0] == "fresh"


def test_ledger_only_submitted_rows_are_indexed(tmp_path):
    source = write(tmp_path / "ledger.json", [row("https://example.com/1", status="REJECTED"), row("https://example.com/2", status="SUBMITTED")])
    idx = DedupeIndex(tmp_path / "index.db", [Source("submitted", source, "ledger")])
    idx.refresh()
    assert idx.check_candidate("other", "other", "https://example.com/1")[0] == "fresh"
    assert idx.check_candidate("other", "other", "https://example.com/2")[1]["kind"] == "duplicate_of_submitted"


def test_missing_source_and_corrupt_source_do_not_declare_fresh(tmp_path):
    idx = index(tmp_path)
    (tmp_path / "queue.json").unlink()
    assert idx.check_candidate("other", "other", "https://example.com/1")[0] == "suspect"
    assert idx.refresh()["complete"] is False
    assert idx.status()["complete"] is False
    (tmp_path / "queue.json").write_text('{"rows":[],"rows":[]}')
    assert idx.refresh()["complete"] is False


def test_unresolved_rows_keep_positive_proof_but_prevent_fresh_claim(tmp_path):
    idx = index(tmp_path, [row("https://example.com/one"), row("", role_id="unknown")])
    assert idx.status()["complete"] is False
    verdict, evidence = idx.check_candidate("other", "other", "https://example.com/one")
    assert verdict == "duplicate" and evidence["coverage_complete"] is False
    assert idx.check_candidate("other", "other", "https://example.com/two")[0] == "suspect"
    batch = idx.check_batch([row("https://example.com/one"), row("https://example.com/two")])
    assert [v for v, _ in batch] == ["duplicate", "suspect"]


def test_missing_candidate_identity_is_not_confidently_fresh(tmp_path):
    idx = index(tmp_path)
    assert idx.check_candidate("other", "other", "")[1]["kind"] == "candidate_identity_missing"
    assert idx.check_batch([{}])[0][0] == "suspect"


def test_corrupt_and_missing_index_are_advisory(tmp_path):
    idx = index(tmp_path)
    (tmp_path / "index.sqlite3").write_bytes(b"corrupt")
    assert idx.check_candidate("other", "other", "https://example.com/1")[1]["kind"] == "index_unavailable"
    (tmp_path / "index.sqlite3").unlink()
    assert idx.check_candidate("other", "other", "https://example.com/1")[0] == "suspect"


def test_changed_source_and_replay_remove_old_identity_atomically(tmp_path):
    idx = index(tmp_path, [row("https://example.com/one")])
    before = idx.status()["revision"]
    stat = (tmp_path / "queue.json").stat()
    write(tmp_path / "queue.json", [row("https://example.com/two")])
    os.utime(tmp_path / "queue.json", ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert idx.check_candidate("other", "other", "https://example.com/one")[1]["kind"] == "source_revision_changed"
    after = idx.refresh()["revision"]
    assert before != after
    assert idx.refresh()["revision"] == after
    assert idx.check_candidate("other", "other", "https://example.com/one")[0] == "fresh"
    assert idx.check_candidate("other", "other", "https://example.com/two")[0] == "duplicate"


def test_restart_reuses_durable_snapshot(tmp_path):
    idx = index(tmp_path, [row("https://example.com/one")])
    reopened = DedupeIndex(idx.path, idx.sources)
    assert reopened.status() == idx.status()
    assert reopened.check_candidate("other", "other", "https://example.com/one")[0] == "duplicate"


def test_expiry_clock_rollback_and_configuration_change_hold(tmp_path):
    now = [100]
    idx = index(tmp_path, clock=lambda: now[0], max_age_seconds=10)
    now[0] = 111
    assert idx.check_candidate("other", "other", "https://example.com/one")[1]["kind"] == "index_expired"
    now[0] = 99
    assert idx.status()["reason"] == "index_expired"
    changed = DedupeIndex(idx.path, [Source("new-id", str(tmp_path / "queue.json"))])
    assert changed.status()["reason"] == "index_configuration_changed"


def test_alias_requires_bound_authentication_and_supports_revocation(tmp_path):
    left, right = "https://jobs.lever.co/acme/one", "https://example.com/reposted/one"
    idx = index(tmp_path, [row(left)])
    with pytest.raises(PermissionError):
        idx.review_alias(left, right, evidence_ref="review:1", credential=True)
    idx.authenticate_review = lambda payload, credential: True
    with pytest.raises(PermissionError):
        idx.review_alias(left, right, evidence_ref="review:1", credential=True)
    requests = []
    def authenticate(payload, credential):
        requests.append(payload)
        return "operator-1" if credential == "host-checked-token" else None
    idx.authenticate_review = authenticate
    idx.review_alias(left, right, evidence_ref="review:1", credential="host-checked-token")
    assert requests[0]["source_config_revision"] == idx.config_revision
    assert requests[0]["action"] == "accept"
    assert idx.check_candidate("other", "other", right)[1]["match"] == "reviewed_alias"
    idx.review_alias(left, right, evidence_ref="review:2", credential="host-checked-token", action="revoke")
    assert idx.check_candidate("other", "other", right)[0] == "fresh"


def test_alias_is_not_transitive_and_does_not_survive_config_change(tmp_path):
    a, b, c = ("https://example.com/job/" + x for x in ("a", "b", "c"))
    idx = index(tmp_path, [row(a)], authenticate_review=lambda p, c: "operator")
    idx.review_alias(a, b, evidence_ref="review:1", credential=None)
    idx.review_alias(b, c, evidence_ref="review:2", credential=None)
    assert idx.check_candidate("other", "other", c)[0] == "fresh"
    changed = DedupeIndex(idx.path, [Source("new-id", str(tmp_path / "queue.json"))])
    changed.refresh()
    assert changed.check_candidate("other", "other", b)[0] == "fresh"


@pytest.mark.parametrize("url", [None, 12, "https://example.com:bad/a", "ftp://example.com/a", "https://user:pass@example.com/a", "javascript:alert(1)", "https://example.com/a\n"])
def test_invalid_urls_do_not_crash_or_produce_identity(url):
    assert canonical_url(url) == ""


def test_source_bounds_invalid_shapes_and_symlinks(tmp_path, monkeypatch):
    import dedupe_index as module
    idx = index(tmp_path)
    monkeypatch.setattr(module, "MAX_ROWS", 1)
    write(tmp_path / "queue.json", [row("https://example.com/one"), row("https://example.com/two")])
    assert idx.refresh()["complete"] is False
    write(tmp_path / "queue.json", [row("https://example.com/one", ats_url=7)])
    assert idx.refresh()["complete"] is False
    (tmp_path / "queue.json").unlink()
    (tmp_path / "queue.json").symlink_to(write(tmp_path / "actual.json", []))
    assert idx.refresh()["complete"] is False


def test_explicit_missing_registry_is_not_default_registry(tmp_path):
    with pytest.raises(FileNotFoundError):
        get_index(tmp_path, config_path=tmp_path / "no-such-config.json")


def test_stage_reserves_no_identity_before_acceptance(tmp_path):
    idx = index(tmp_path)
    seen = set()
    first = row("https://jobs.lever.co/acme/one")
    result = stage_verdict(first, idx, seen, set())
    assert not seen
    seen.update(result[2])  # Host triage adds keys only after validation succeeds.
    second = row("https://jobs.lever.co/acme/one/apply", role_id="another-id")
    assert stage_verdict(second, idx, seen, set())[1]["kind"] == "duplicate_in_batch"
    distinct = row("https://jobs.lever.co/acme/two", role_id="third-id")
    assert stage_verdict(distinct, idx, seen, set())[0] != "duplicate"


def test_gate_preserves_uncertainty_on_unavailable_index(tmp_path, monkeypatch):
    import dedupe_gate
    monkeypatch.setattr(dedupe_gate, "LEDGER", str(tmp_path / "missing-ledger"))
    monkeypatch.setattr(dedupe_gate, "STANDARD_QUEUE", str(tmp_path / "missing-queue"))
    verdict, evidence = dedupe_gate.check_candidate("other", "other", "https://example.com/one")
    assert verdict == "suspect"
    fresh, dupes = dedupe_gate.filter_batch([row("https://example.com/one")])
    assert len(fresh) == 1 and not dupes
    assert fresh[0]["dedupe_advisory"]["execution_authorized"] is False


def test_queue_intake_passes_every_url_field(monkeypatch):
    import dedupe_gate
    import queue_intake
    observed = []
    def check(company, title, url, **kwargs):
        observed.append(kwargs["urls"])
        return "fresh", {}
    monkeypatch.setattr(dedupe_gate, "check_candidate", check)
    candidate = row("https://example.com/one", application_url="https://example.com/two")
    queue_intake.validate_batch([candidate])
    assert observed == [candidate]


def test_triage_does_not_let_invalid_row_poison_later_identity(tmp_path, monkeypatch):
    import staging_ingest
    idx = index(tmp_path)
    monkeypatch.setattr(staging_ingest, "_title_triage", None)
    monkeypatch.setattr(staging_ingest, "_work_auth_parked", lambda *args: False)
    monkeypatch.setattr(staging_ingest, "blocklisted", lambda *args: False)
    monkeypatch.setattr(staging_ingest, "validate_entry", lambda e: (["invalid"] if e["role_id"] == "bad" else [], []))
    first = row("https://jobs.lever.co/acme/one", role_id="bad")
    second = row("https://jobs.lever.co/acme/one/apply", role_id="good")
    third = row("https://jobs.lever.co/acme/one", role_id="duplicate")
    accepted, rejected = staging_ingest.triage([first, second, third], set(), set(), set(), index=idx)
    assert [e["role_id"] for e in accepted] == ["good"]
    assert [rid for rid, _ in rejected] == ["bad", "duplicate"]


def test_emission_precheck_keeps_distinct_same_title_and_blocks_exact(monkeypatch):
    import staging_ingest
    monkeypatch.setattr(staging_ingest, "validate_entry", lambda e: ([], []))
    candidates = [row("https://jobs.lever.co/acme/one", role_id="a"),
                  row("https://jobs.lever.co/acme/two", role_id="b"),
                  row("https://jobs.lever.co/acme/one/apply", role_id="c")]
    accepted, withheld, report = staging_ingest.emission_precheck(candidates)
    assert [e["role_id"] for e in accepted] == ["a", "b"]
    assert withheld == [("c", "precheck: duplicate posting identity in batch")]


def test_batch_checks_hashes_twice_instead_of_once_per_candidate(tmp_path, monkeypatch):
    import dedupe_index
    idx = index(tmp_path, [row("https://example.com/one")])
    actual, calls = dedupe_index._regular_bytes, []
    def capture(*args, **kwargs):
        calls.append(args)
        return actual(*args, **kwargs)
    monkeypatch.setattr(dedupe_index, "_regular_bytes", capture)
    results = idx.check_batch([row("https://example.com/one")] * 10)
    assert all(v == "duplicate" for v, _ in results)
    assert len(calls) == 2


def test_source_change_during_batch_downgrades_every_result(tmp_path, monkeypatch):
    idx = index(tmp_path, [row("https://example.com/one")])
    actual = idx._lookup
    def changing(*args):
        result = actual(*args)
        write(tmp_path / "queue.json", [])
        return result
    monkeypatch.setattr(idx, "_lookup", changing)
    results = idx.check_batch([row("https://example.com/one")])
    assert results[0][0] == "suspect"
    assert results[0][1]["kind"] == "source_revision_changed"


def test_source_change_during_single_lookup_downgrades_match(tmp_path, monkeypatch):
    idx = index(tmp_path, [row("https://example.com/one")])
    actual = idx._lookup
    def changing(*args):
        result = actual(*args)
        write(tmp_path / "queue.json", [])
        return result
    monkeypatch.setattr(idx, "_lookup", changing)
    assert idx.check_candidate("other", "other", "https://example.com/one")[1]["kind"] == "source_revision_changed"


def test_private_database_and_sidecar_paths_are_required(tmp_path):
    idx = index(tmp_path)
    idx.path.chmod(0o644)
    assert idx.status()["complete"] is False
    idx.path.chmod(0o600)
    journal = Path(str(idx.path) + "-journal")
    journal.symlink_to(tmp_path / "queue.json")
    assert idx.status()["complete"] is False
    journal.unlink()
    tmp_path.chmod(0o777)
    assert idx.status()["complete"] is False
    tmp_path.chmod(0o700)


def test_unconfigured_nonunderscore_queue_cannot_suppress_role_id(tmp_path):
    import staging_ingest
    write(tmp_path / "old-queue.json", [row("https://example.com/one", role_id="old")])
    write(tmp_path / "standard-queue.json", [])
    errors = []
    ids, _ = staging_ingest.load_queue_keys(str(tmp_path), errors=errors)
    assert "old" not in ids and not errors
    ids, _ = staging_ingest.load_queue_keys(str(tmp_path), paths=[str(tmp_path / "old-queue.json")])
    assert "old" in ids
    (tmp_path / "standard-queue.json").unlink()
    staging_ingest.load_queue_keys(str(tmp_path), errors=errors)
    assert errors and errors[0]["reason"] == "FileNotFoundError"


def test_filter_batch_uses_exact_intrabatch_identity(tmp_path):
    idx = index(tmp_path)
    candidates = [row("https://jobs.lever.co/acme/one", role_id="a"),
                  row("https://jobs.lever.co/acme/two", role_id="b"),
                  row("https://jobs.lever.co/acme/one/apply", role_id="c")]
    accepted, withheld = filter_batch(candidates, index=idx)
    assert [e["role_id"] for e in accepted] == ["a", "b"]
    assert [e["role_id"] for e in withheld] == ["c"]


@pytest.mark.parametrize("field", ["rows", "entries", "items", "leads"])
def test_supported_queue_envelopes(tmp_path, field):
    idx = index(tmp_path)
    write(tmp_path / "queue.json", {field: [row("https://example.com/one")]})
    assert idx.refresh()["complete"]
    assert idx.check_candidate("other", "other", "https://example.com/one")[0] == "duplicate"
