"""Scoped answer reuse preserving the compound-growth directive (2026-09-21).

Covers engines/answer_reuse.py (ReuseSession) and the additive
``registry`` parameter on answer_resolver.resolve/_parse_free_scope/
resolve_bank:

  - scope-mismatch -> no reuse (abstain + abstain evidence)
  - in-scope -> reuse with evidence log (resolved + evidence metadata)
  - compound property: the employer registry is loaded EXACTLY ONCE per
    session no matter how many legacy answers are reused (first answer
    costs once; every reuse after is pure in-memory work, zero per-reuse
    I/O). Bare resolve() calls keep the historical per-call behavior —
    the leak the session closes, proven by the counting tests.
  - evidence log integrity (monotonic seq, immutable copies, no values)
  - fail-closed session behavior; TOCTOU binding snapshot.

Conventions mirror test_bank_scope.py: tests live alongside the engines,
sys.path extended to the engines dir. No queue/ledger/telemetry writes
anywhere in these tests (the module performs none by design).
"""
import dataclasses
import os
import sys

import pytest

ENGINES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engines")
sys.path.insert(0, ENGINES)  # tests now live in tests/; engines stay in engines/

import answer_resolver as ar  # noqa: E402
import answer_reuse as aur  # noqa: E402


# ------------------------------------------------------------------ fixtures

LEGACY_SAMSARA = {
    "answer": "Yes",
    "scope": "Samsara applications",
    "provenance": "the applicant's own words",
}
MACHINE_ACME = {"value": "Yes", "scope": "employer:Acme"}
MACHINE_GLOBAL = {"value": "Yes", "scope": "global"}
MACHINE_AMBIGUOUS = {"value": "Yes", "scope": "ambiguous"}
MACHINE_REFUSAL = {
    "value": "DO NOT CERTIFY - route to PERSONAL TAKEOVER",
    "scope": "global",
}

BANK = {
    # "relocation_willingness" is on the resolver's safe-legacy allowlist,
    # so legacy-scope entries under it can resolve (flagged/exact).
    "relocation_willingness": LEGACY_SAMSARA,
    "relocation_willingness_2": dict(LEGACY_SAMSARA),
    "relocation_willingness_3": dict(LEGACY_SAMSARA),
    "acme_only_fact": MACHINE_ACME,
    "standing_fact": MACHINE_GLOBAL,
    "quarantined_fact": MACHINE_AMBIGUOUS,
    "refusal_fact": MACHINE_REFUSAL,
}


def _counting_registry(names):
    """A known_employers replacement that counts invocations."""
    calls = {"n": 0}

    def _fake():
        calls["n"] += 1
        return set(names)

    _fake.calls = calls
    return _fake


@pytest.fixture()
def samsara_session(monkeypatch):
    """ReuseSession for employer Samsara with a 1-name registry snapshot."""
    fake = _counting_registry({"Samsara"})
    monkeypatch.setattr(aur, "_known_employers", fake)
    sess = aur.ReuseSession(BANK, employer="Samsara",
                            role_context={"company": "Samsara",
                                          "role_id": "r-1"})
    sess._registry_fake = fake  # test introspection only
    return sess


# ------------------------------------------------------- scope verification

def test_scope_mismatch_no_reuse(samsara_session):
    """employer:Acme answer requested by Samsara -> ABSTAIN, never reused."""
    res = samsara_session.reuse("acme_only_fact")
    assert res.status == ar.STATUS_ABSTAIN
    assert res.value == ""
    assert "never crosses employers" in res.reason
    ev = samsara_session.evidence()[-1]
    assert ev.key == "acme_only_fact"
    assert ev.status == ar.STATUS_ABSTAIN
    assert ev.employer == "Samsara"


def test_scope_mismatch_legacy_free_text(monkeypatch):
    """Legacy 'Samsara applications' text requested by Acme -> ABSTAIN."""
    fake = _counting_registry({"Samsara"})
    monkeypatch.setattr(aur, "_known_employers", fake)
    sess = aur.ReuseSession(BANK, employer="Acme",
                            role_context={"company": "Acme"})
    res = sess.reuse("relocation_willingness")
    assert res.status == ar.STATUS_ABSTAIN
    assert "never crosses employers" in res.reason
    assert fake.calls["n"] == 1  # still one registry load per session


def test_in_scope_machine_employer_reuses(samsara_session):
    """employer:Acme answer IS reusable inside Acme's own session."""
    sess = aur.ReuseSession(BANK, employer="Acme",
                            role_context={"company": "Acme"})
    res = sess.reuse("acme_only_fact")
    assert res.status == ar.STATUS_RESOLVED
    assert res.value == "Yes"
    assert res.scope == "employer:Acme"
    ev = sess.evidence()[-1]
    assert (ev.key, ev.status, ev.scope, ev.employer) == (
        "acme_only_fact", ar.STATUS_RESOLVED, "employer:Acme", "Acme")


def test_in_scope_global_reuses_with_evidence(samsara_session):
    res = samsara_session.reuse("standing_fact")
    assert res.status == ar.STATUS_RESOLVED
    assert res.value == "Yes"
    ev = samsara_session.evidence()[-1]
    assert ev.scope == "global"
    assert ev.source == ""  # no provenance recorded on this entry
    assert ev.seq == 1
    assert ev.reused_at  # UTC ISO timestamp present


def test_in_scope_legacy_free_text_resolves(samsara_session):
    """Legacy 'Samsara applications' + Samsara requester + canonical
    binding -> resolved under employers-exact scope, evidence-logged."""
    res = samsara_session.reuse("relocation_willingness")
    assert res.status == ar.STATUS_RESOLVED
    assert res.value == "Yes"
    assert res.scope == "employers-exact:Samsara"
    ev = samsara_session.evidence()[-1]
    assert ev.source == "the applicant's own words"


def test_ambiguous_never_reuses(samsara_session):
    res = samsara_session.reuse("quarantined_fact")
    assert res.status == ar.STATUS_ABSTAIN
    assert "quarantined" in res.reason


def test_refusal_value_never_reuses(samsara_session):
    """A banked personal-takeover refusal is not an answer."""
    res = samsara_session.reuse("refusal_fact")
    assert res.status == ar.STATUS_ABSTAIN
    assert "personal" in res.reason.lower()


def test_missing_key_abstains_and_is_logged(samsara_session):
    res = samsara_session.reuse("no_such_key")
    assert res.status == ar.STATUS_ABSTAIN
    ev = samsara_session.evidence()[-1]
    assert ev.key == "no_such_key" and ev.status == ar.STATUS_ABSTAIN


# ------------------------------------------------------- compound property

def test_registry_loaded_once_per_session(samsara_session):
    """First answer costs once: N legacy reuses -> exactly 1 registry load.

    This is the compound-growth property: reuse N answers, pay the
    registry-read cost once (at construction), never per reuse.
    """
    fake = samsara_session._registry_fake
    assert fake.calls["n"] == 1  # construction only
    for k in ("relocation_willingness", "relocation_willingness_2",
              "relocation_willingness_3", "standing_fact", "acme_only_fact"):
        samsara_session.reuse(k)
    assert fake.calls["n"] == 1, (
        "per-reuse registry re-read: the compound-growth property is broken")
    rep = samsara_session.cost_report()
    assert rep["registry_loads"] == 1
    assert rep["reuses"] == 5
    assert rep["evidence_entries"] == 5


def test_bare_resolve_still_reads_registry_per_call(monkeypatch):
    """Documents the leak the session closes: bare resolve() (no session)
    performs one registry read per legacy resolution — the historical,
    unchanged behavior. The session exists precisely to stop paying it."""
    fake = _counting_registry({"Samsara"})
    monkeypatch.setattr(ar, "_known_employers", fake)
    for _ in range(3):
        ar.resolve("relocation_willingness", LEGACY_SAMSARA,
                   employer="Samsara",
                   role_context={"company": "Samsara", "role_id": "r-1"})
    assert fake.calls["n"] == 3


def test_resolver_registry_kwarg_uses_snapshot(monkeypatch):
    """The additive registry= kwarg replaces the per-call read with the
    caller-supplied snapshot — identical resolutions, zero extra I/O."""
    fake = _counting_registry({"Samsara"})
    monkeypatch.setattr(ar, "_known_employers", fake)
    res = ar.resolve("relocation_willingness", LEGACY_SAMSARA,
                     employer="Samsara",
                     role_context={"company": "Samsara", "role_id": "r-1"},
                     registry={"Samsara"})
    assert res.status == ar.STATUS_RESOLVED
    assert res.scope == "employers-exact:Samsara"
    assert fake.calls["n"] == 0


def test_resolve_bank_registry_kwarg(monkeypatch):
    fake = _counting_registry({"Samsara"})
    monkeypatch.setattr(ar, "_known_employers", fake)
    out = ar.resolve_bank({"k1": LEGACY_SAMSARA, "k2": MACHINE_GLOBAL},
                          employer="Samsara",
                          role_context={"company": "Samsara"},
                          registry={"Samsara"})
    assert out["k1"].status == ar.STATUS_RESOLVED
    assert out["k2"].status == ar.STATUS_RESOLVED
    assert fake.calls["n"] == 0
    # and without the kwarg, historical behavior is unchanged
    out2 = ar.resolve_bank({"k1": LEGACY_SAMSARA}, employer="Samsara",
                           role_context={"company": "Samsara"})
    assert out2["k1"].status == ar.STATUS_RESOLVED
    assert fake.calls["n"] == 1


def test_reuse_many_batch_and_cost_report(samsara_session):
    out = samsara_session.reuse_many(
        ["standing_fact", "acme_only_fact", "no_such_key"])
    assert set(out) == {"standing_fact", "acme_only_fact", "no_such_key"}
    assert out["standing_fact"].status == ar.STATUS_RESOLVED
    assert out["acme_only_fact"].status == ar.STATUS_ABSTAIN
    rep = samsara_session.cost_report()
    assert rep["reuses"] == 3
    assert rep["resolved"] == 1
    assert rep["abstained"] == 2
    assert rep["registry_loads"] == 1
    assert rep["evidence_entries"] == 3


# ------------------------------------------------------- evidence integrity

def test_evidence_seq_monotonic_and_immutable(samsara_session):
    samsara_session.reuse_many(["standing_fact", "acme_only_fact"])
    ev = samsara_session.evidence()
    assert [e.seq for e in ev] == [1, 2]
    assert isinstance(ev, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev[0].status = "resolved"  # evidence records are frozen
    # a second read returns an equal but independent tuple
    assert samsara_session.evidence() == ev
    assert samsara_session.evidence() is not ev


def test_evidence_carries_no_answer_values(samsara_session):
    """Evidence proves authority (key/status/scope/source), never the value."""
    samsara_session.reuse("standing_fact")
    fields = {f.name for f in dataclasses.fields(aur.ReuseEvidence)}
    assert "value" not in fields
    ev = samsara_session.evidence()[-1]
    assert "Yes" not in dataclasses.astuple(ev)


# ------------------------------------------------------- session hardening

def test_binding_snapshot_toctou(samsara_session):
    """Mutating the caller's role_context after construction cannot change
    resolutions inside the session (TOCTOU-hardened like the resolver)."""
    rctx = {"company": "Samsara", "role_id": "r-1"}
    sess = aur.ReuseSession(BANK, employer="Samsara", role_context=rctx)
    rctx["company"] = "Acme"  # attacker/caller mutation after construction
    rctx["role_id"] = "r-9"
    res = sess.reuse("relocation_willingness")
    assert res.status == ar.STATUS_RESOLVED
    assert res.scope == "employers-exact:Samsara"


def test_session_fail_closed_on_broken_answers(monkeypatch):
    """A hostile answers mapping (raises on .get) still yields ABSTAIN."""
    fake = _counting_registry({"Samsara"})
    monkeypatch.setattr(aur, "_known_employers", fake)

    class EvilDict(dict):
        def get(self, k, default=None):
            raise RuntimeError("boom")

    sess = aur.ReuseSession(EvilDict(), employer="Samsara",
                            role_context={"company": "Samsara"})
    # dict(EvilDict()) copies entries at construction; force the failure
    # path through reuse by breaking lookup after construction instead:
    sess._answers = EvilDict()
    res = sess.reuse("standing_fact")
    assert res.status == ar.STATUS_ABSTAIN
    ev = sess.evidence()[-1]
    assert ev.status == ar.STATUS_ABSTAIN


def test_session_registry_failure_degrades_to_abstain(monkeypatch):
    """An unreadable registry at construction degrades legacy detection
    toward abstain — never toward an unauthorized resolve."""

    def _boom():
        raise OSError("queues unreadable")

    monkeypatch.setattr(aur, "_known_employers", _boom)
    sess = aur.ReuseSession(BANK, employer="Samsara",
                            role_context={"company": "Samsara"})
    res = sess.reuse("relocation_willingness")  # legacy free-text path
    # empty snapshot: no name detectable -> cannot resolve employers-exact;
    # degrades to flagged-legacy/abstain, never to an unauthorized resolve
    assert res.status != ar.STATUS_RESOLVED
    # machine scopes never consulted the registry: still resolve
    res2 = sess.reuse("standing_fact")
    assert res2.status == ar.STATUS_RESOLVED
