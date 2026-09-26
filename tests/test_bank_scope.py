"""Governed memory (ADOPTION 1 of 5): scoped bank writes.

New bank entries are stored as {"value": <str>, "scope": <scope>} with
scope in {"global", "employer:<Name>", "ambiguous"}. Consent/attestation
families and employer-named questions WITHOUT an explicit --scope infer
"ambiguous" and are written to _quarantined, never to answers -- a
per-employer consent can never land in global answers again (the
Silver Oak/Paylocity SMS-consent incident this closes).

Legacy entries (plain strings, old {"answer": ...} dicts) are untouched
and resolve via bank_scope.bank_value()/bank_scope().
"""
import json
import os
import sys
import tempfile

import pytest

ENGINES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engines")
sys.path.insert(0, ENGINES)  # tests now live in tests/; engines stay in engines/

import bank_scope as bs  # noqa: E402
import tray_answer  # noqa: E402
import input_tray_digest as tray  # noqa: E402


STAMP = "2026-09-16 21:30 PDT"


def _bank_file(tmp, seed=None):
    path = os.path.join(tmp, "answer_bank.json")
    bank = {"answers": {}, "_provenance": {}, "_quarantined": {}}
    if seed:
        bank["answers"].update(seed)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(bank, f)
    return path


CONSENT_CARD = {
    "family": "consent/whatsapp",
    "question": ("Do you consent to receive text messages about your "
                 "application?"),
    "key": "k-consent-1",
}

ATTEST_CARD = {
    "family": "attestation/general",
    "question": "Do you attest that you have read the arbitration agreement?",
    "key": "k-attest-1",
}

TRAVEL_CARD = {
    "family": "travel_commitment/general",
    "question": "TRAVEL DECISION REQUIRED — generic travel parks for judgment",
    "key": "k-travel-1",
}


# ---------------------------------------------------------------- helpers


def test_bank_value_legacy_plain_string():
    assert bs.bank_value("Yes") == "Yes"
    assert bs.bank_scope("Yes") == "legacy-unknown"


def test_bank_value_old_dict_form():
    entry = {"answer": "Yes", "question_patterns": ["consent to ai"],
             "provenance": "the applicant's own words"}
    assert bs.bank_value(entry) == "Yes"
    assert bs.bank_scope(entry) == "legacy-unknown"


def test_bank_value_new_scoped_shape():
    entry = {"value": "Yes", "scope": "employer:Acme"}
    assert bs.bank_value(entry) == "Yes"
    assert bs.bank_scope(entry) == "employer:Acme"
    assert bs.bank_scope({"value": "Yes", "scope": "global"}) == "global"


def test_bank_value_fail_safe_shapes():
    assert bs.bank_value(None) == ""
    assert bs.bank_value({}) == ""
    assert bs.bank_scope(None) == "legacy-unknown"
    assert bs.bank_scope({"scope": "bogus"}) == "legacy-unknown"


def test_bank_helpers_on_real_bank():
    """Every entry in the operator's answer bank resolves without error."""
    from keel_paths import DATA  # noqa: E402
    path = os.path.join(DATA, "answer_bank.json")
    if not os.path.exists(path):
        pytest.skip("no answer_bank.json in this repo checkout")
    bank = json.load(open(path, encoding="utf-8"))
    for key, entry in bank["answers"].items():
        assert isinstance(bs.bank_value(entry), str), key
        scope = bs.bank_scope(entry)
        assert scope == "legacy-unknown" or scope == "global" \
            or scope.startswith("employer:"), (key, scope)


# ---------------------------------------------------------------- decisions


def test_decide_scope_consent_family_ambiguous():
    scope, reason = bs.decide_bank_scope(None, CONSENT_CARD, employers=set())
    assert scope == "ambiguous"
    assert reason  # reason recorded for the quarantine entry


def test_decide_scope_attestation_family_ambiguous():
    scope, _ = bs.decide_bank_scope(None, ATTEST_CARD, employers=set())
    assert scope == "ambiguous"


def test_decide_scope_employer_named_question_ambiguous():
    card = {"family": "availability/general",
            "question": "Would you accept an offer from Acme to relocate?",
            "key": "k-1"}
    scope, reason = bs.decide_bank_scope(None, card, employers={"Acme"})
    assert scope == "ambiguous"
    assert "Acme" in reason


def test_decide_scope_plain_fact_global():
    scope, reason = bs.decide_bank_scope(None, TRAVEL_CARD, employers=set())
    assert scope == "global"
    assert reason == ""


def test_decide_scope_explicit_wins():
    scope, _ = bs.decide_bank_scope("global", CONSENT_CARD, employers=set())
    assert scope == "global"
    scope, _ = bs.decide_bank_scope("employer:Acme", CONSENT_CARD,
                                    employers=set())
    assert scope == "employer:Acme"
    scope, _ = bs.decide_bank_scope("ambiguous", TRAVEL_CARD, employers=set())
    assert scope == "ambiguous"


def test_parse_scope_arg_rejects_garbage():
    with pytest.raises(ValueError):
        bs.parse_scope_arg("employer:")
    with pytest.raises(ValueError):
        bs.parse_scope_arg("universe")


# ---------------------------------------------------------------- write path


def test_write_quarantines_consent_without_scope():
    """(a) ambiguous consent/attestation card + --bank-new -> quarantined."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _bank_file(tmp)
        banked, quarantined, scope = tray_answer.apply_bank_write(
            path, "sms_consent_x", "Yes", None, CONSENT_CARD, STAMP,
            employers=set())
        assert not banked and quarantined and scope == "ambiguous"
        bank = json.load(open(path))
        assert "sms_consent_x" not in bank["answers"]
        q = bank["_quarantined"]["sms_consent_x"]
        assert q["was"] == "Yes"
        assert q["reason"]
        assert "quarantined_at" in q
        # provenance records the failed promotion attempt
        assert bank["_provenance"]["sms_consent_x"]["scope"] == "ambiguous"


def test_write_employer_scope_accepted():
    """(b) --scope employer:Acme -> accepted with scope recorded."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _bank_file(tmp)
        banked, quarantined, scope = tray_answer.apply_bank_write(
            path, "sms_consent_acme", "Yes", "employer:Acme", CONSENT_CARD,
            STAMP, employers=set())
        assert banked and not quarantined
        bank = json.load(open(path))
        entry = bank["answers"]["sms_consent_acme"]
        assert entry["value"] == "Yes"
        assert entry["scope"] == "employer:Acme"
        # 2026-09-17 tray-answer compounding: the answered question and card
        # key persist with the banked answer so the prescreen can match the
        # exact tray card later.
        assert entry["question"] == CONSENT_CARD["question"]
        assert entry["card_key"] == CONSENT_CARD["key"]
        assert isinstance(entry["question_variants"], list)
        assert bank["_provenance"]["sms_consent_acme"]["scope"] == \
            "employer:Acme"


def test_write_explicit_global_accepted():
    """(c) --scope global explicit -> accepted as global."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _bank_file(tmp)
        banked, quarantined, scope = tray_answer.apply_bank_write(
            path, "travel_willingness", "Up to 25% defined travel",
            "global", TRAVEL_CARD, STAMP, employers=set())
        assert banked and not quarantined and scope == "global"
        bank = json.load(open(path))
        entry = bank["answers"]["travel_willingness"]
        assert entry["value"] == "Up to 25% defined travel"
        assert entry["scope"] == "global"
        assert entry["question"] == TRAVEL_CARD["question"]
        assert entry["card_key"] == TRAVEL_CARD["key"]
        assert isinstance(entry["question_variants"], list)


def test_write_fail_safe_quarantines_on_scope_error(monkeypatch):
    """Scope-check failure must never crash: fail to quarantine + continue."""
    def _boom(*a, **k):
        raise RuntimeError("classifier exploded")
    monkeypatch.setattr(bs, "decide_bank_scope", _boom)
    with tempfile.TemporaryDirectory() as tmp:
        path = _bank_file(tmp)
        banked, quarantined, scope = tray_answer.apply_bank_write(
            path, "overtime_x", "Yes", None, TRAVEL_CARD, STAMP,
            employers=set())
        assert not banked and quarantined and scope == "ambiguous"
        bank = json.load(open(path))
        assert "overtime_x" not in bank["answers"]
        assert "scope-check failed" in bank["_quarantined"]["overtime_x"]["reason"]


# ---------------------------------------------------------------- end-to-end


def _setup_e2e(tmp):
    qdir = os.path.join(tmp, "queue")
    hdir = os.path.join(tmp, "hidden_files")
    os.makedirs(qdir)
    os.makedirs(hdir)
    for mod in (tray, tray_answer):
        mod.QDIR = qdir
    tray.HDIR = hdir
    tray.WM = os.path.join(hdir, "input-tray-logged.json")
    tray.TRAY_JSON = os.path.join(hdir, "input-tray.json")
    tray.FAM_HIST = os.path.join(hdir, "input-tray-families.json")
    tray.BANK = os.path.join(hdir, "answer_bank.json")
    tray_answer.NI_Q = os.path.join(qdir, "needs_input-queue.json")
    tray_answer.STD_Q = os.path.join(qdir, "standard-queue.json")
    tray_answer.ANSWERS_LOG = os.path.join(hdir, "tray-answers.jsonl")
    tray_answer.BASE = tmp
    consent = ("FAMILY[sms_consent/general]: SMS CONSENT DECISION REQUIRED — "
               "\"Do you consent to receive text messages about your "
               "application?\" [3 applications]")
    leads = [{
        "role_id": "R1", "employer": "Acme", "title": "Ops Manager",
        "fit_score": 80, "status": "PARKED-NEEDS-INPUT",
        "status_updated": "2026-09-16 10:00 PDT",
        "unresolved": [consent], "queue_notes": "",
    }]
    with open(os.path.join(qdir, "needs_input-queue.json"), "w") as f:
        json.dump(leads, f)
    with open(os.path.join(qdir, "standard-queue.json"), "w") as f:
        json.dump([], f)
    with open(tray.BANK, "w") as f:
        json.dump({"answers": {"overtime_willingness": "Yes"},
                   "_provenance": {}, "_quarantined": {}}, f)


def test_e2e_consent_family_never_promotes_to_global(monkeypatch, capsys):
    """(e) consent family + --bank-new WITHOUT --scope -> quarantined,
    unblock routing still works."""
    monkeypatch.setattr(bs, "known_employers", lambda *a: set())
    with tempfile.TemporaryDirectory() as tmp:
        _setup_e2e(tmp)
        cards = tray.collect_cards()
        key = next(c["key"] for c in cards.values()
                   if c["family"] == "sms_consent/general")
        rc = tray_answer.main(["--live", "--key", key,
                               "--answer", "Yes",
                               "--bank", tray.BANK,
                               "--bank-new", "sms_consent_acme"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "QUARANTINED" in out
        bank = json.load(open(tray.BANK))
        # (e) the money assertion: never in answers
        assert "sms_consent_acme" not in bank["answers"]
        assert bank["_quarantined"]["sms_consent_acme"]["was"] == "Yes"
        # unblock routing unaffected: the lead resolved
        q = json.load(open(tray_answer.NI_Q))
        assert q[0]["unresolved"] == []


def test_e2e_explicit_employer_scope_banked(monkeypatch):
    """(b) end-to-end: --scope employer:Acme banks with scope recorded."""
    monkeypatch.setattr(bs, "known_employers", lambda *a: set())
    with tempfile.TemporaryDirectory() as tmp:
        _setup_e2e(tmp)
        cards = tray.collect_cards()
        key = next(c["key"] for c in cards.values()
                   if c["family"] == "sms_consent/general")
        rc = tray_answer.main(["--live", "--key", key,
                               "--answer", "Yes",
                               "--bank", tray.BANK,
                               "--bank-new", "sms_consent_acme",
                               "--scope", "employer:Acme"])
        assert rc == 0
        bank = json.load(open(tray.BANK))
        entry = bank["answers"]["sms_consent_acme"]
        assert entry["value"] == "Yes"
        assert entry["scope"] == "employer:Acme"
        # 2026-09-17 tray-answer compounding: the answered question and card
        # key persist with the banked answer so the prescreen can match the
        # exact tray card later.
        assert entry["question"]
        assert entry["card_key"]
        assert isinstance(entry["question_variants"], list)
        assert "sms_consent_acme" not in bank["_quarantined"]
