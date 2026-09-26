import base64
import hashlib
import stat

import pytest

from keel_loki.forms import bind_fixture
from keel_operational.common import digest
from keel_operational.fixture import export_fixture
from keel_operational.qualification import fixture_inputs, freeze_plan


def test_accessible_fixture_and_exact_approved_bytes(tmp_path):
    plan = freeze_plan(**fixture_inputs())
    home = tmp_path / 'fixture'
    report = export_fixture(home, plan, expected_plan_sha256=digest(plan))
    page = (home / 'fixture.html').read_text()
    assert 'Account: synthetic_candidate' in page
    assert 'Contract SHA-256: ' + digest(plan['fixture']['contract']) in page
    assert 'aria-label="Full name"' in page
    assert 'crypto.subtle.digest' in page and "metadata.sha256 = 'UNKNOWN'" in page
    assert 'type="submit"' not in page and 'fetch(' not in page
    for name, pin in report['files'].items():
        assert hashlib.sha256((home / name).read_bytes()).hexdigest() == pin
        assert stat.S_IMODE((home / name).stat().st_mode) == 0o600
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert (home / 'fixture_resume.txt').read_bytes() == base64.b64decode(plan['fixture']['attachments']['resume'])
    assert report['execution_authorized'] is False and report['served'] is False
    with pytest.raises(FileExistsError):
        export_fixture(home, plan, expected_plan_sha256=digest(plan))


@pytest.mark.parametrize('fault', ['pin', 'synthetic', 'attachment', 'fixture_hash'])
def test_invalid_fixture_has_no_output(tmp_path, fault):
    plan = freeze_plan(**fixture_inputs())
    pin = digest(plan)
    if fault == 'synthetic':
        plan['fixture']['synthetic'] = False
    elif fault == 'attachment':
        plan['fixture']['attachments']['resume'] = base64.b64encode(b'wrong bytes').decode()
    elif fault == 'fixture_hash':
        plan['fixture_sha256'] = '0' * 64
    else:
        pin = '0' * 64
    if fault != 'pin':
        if fault != 'fixture_hash':
            plan['fixture_sha256'] = digest(plan['fixture'])
        pin = digest(plan)
    with pytest.raises(ValueError):
        export_fixture(tmp_path / 'invalid', plan, expected_plan_sha256=pin)
    assert not (tmp_path / 'invalid').exists()


def test_untrusted_labels_remain_text(tmp_path):
    inputs = fixture_inputs()
    inputs['fixture']['contract']['fields'][0]['label'] = '<script>alert("label")</script>'
    inputs['fixture'] = bind_fixture(inputs['fixture'], now=inputs['now'])
    plan = freeze_plan(**inputs)
    export_fixture(tmp_path / 'escaped', plan, expected_plan_sha256=digest(plan))
    page = (tmp_path / 'escaped' / 'fixture.html').read_text()
    assert '<script>alert(' not in page
    assert '&lt;script&gt;' in page
