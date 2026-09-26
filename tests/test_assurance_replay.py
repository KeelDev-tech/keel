from tools.assurance_replay import replay


def test_replay_contracts_and_no_fake_assurance_on_legacy_path():
    report = replay()
    assert report["all_expectations_met"]
    assert len(report["cases"]) == 10
    assert report["cases"][-1]["actual_assurance_qualified"] is None
    assert not any(c["execution_authorized"] for c in report["cases"])
