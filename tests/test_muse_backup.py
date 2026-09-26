import os
from pathlib import Path
import pytest
from keel_loki.recovery import RecoveryJournal
from keel_muse import backup as b


def runtime(tmp_path):
    home=tmp_path/'runtime';home.mkdir(mode=0o700)
    journal=RecoveryJournal(home,'workspace',now=100)
    return home,journal


def save(tmp_path):
    home,journal=runtime(tmp_path)
    point=b.checkpoint(home,'workspace')
    result=b.snapshot(home,tmp_path/'backup','workspace',expected_checkpoint=point)
    return home,journal,point,result


def test_actual_online_backup_restore_keeps_429(tmp_path):
    assert b.demo(tmp_path/'demo')['rate_limit_preserved'] is True


def test_unknown_and_revoked_approval_survive_restore(tmp_path):
    home,journal=runtime(tmp_path)
    journal.register('job','a'*64,now=101);journal.approve('job','a'*64,'b'*64,now=102)
    lease=journal.claim('job','worker',now=103)
    journal.start('job','worker',lease['fence'],now=104)
    journal.revoke('job',now=105)
    point=b.checkpoint(home,'workspace')
    result=b.snapshot(home,tmp_path/'backup','workspace',expected_checkpoint=point)
    b.restore(tmp_path/'backup',tmp_path/'restored',authoritative_checkpoint=point,expected_manifest_sha256=result['manifest_sha256'])
    state=RecoveryJournal.open_readonly(tmp_path/'restored','workspace').snapshot()['state']
    assert state['jobs']['job']['phase']=='UNKNOWN' and state['jobs']['job']['approval_active'] is False


def test_old_backup_cannot_override_newer_known_head(tmp_path):
    home,journal,point,result=save(tmp_path)
    journal.record_429(now=101)
    current=b.checkpoint(home,'workspace')
    with pytest.raises(ValueError):b.restore(tmp_path/'backup',tmp_path/'restored',authoritative_checkpoint=current,expected_manifest_sha256=result['manifest_sha256'])
    assert not (tmp_path/'restored').exists()


def test_missing_external_checkpoint_and_wrong_manifest_are_denied(tmp_path):
    home,journal,point,result=save(tmp_path)
    with pytest.raises(ValueError):b.restore(tmp_path/'backup',tmp_path/'restored',authoritative_checkpoint=None,expected_manifest_sha256=result['manifest_sha256'])
    with pytest.raises(ValueError):b.restore(tmp_path/'backup',tmp_path/'restored',authoritative_checkpoint=point,expected_manifest_sha256='d'*64)


def test_restore_never_overwrites_destination(tmp_path):
    home,journal,point,result=save(tmp_path);dest=tmp_path/'existing';dest.mkdir();(dest/'precious').write_text('keep')
    with pytest.raises(FileExistsError):b.restore(tmp_path/'backup',dest,authoritative_checkpoint=point,expected_manifest_sha256=result['manifest_sha256'])
    assert (dest/'precious').read_text()=='keep'


def test_only_allowlisted_runtime_files_copied(tmp_path):
    home,journal=runtime(tmp_path);(home/'secret.txt').write_text('private')
    point=b.checkpoint(home,'workspace');result=b.snapshot(home,tmp_path/'backup','workspace',expected_checkpoint=point)
    assert set(result['manifest']['files'])=={'recovery.sqlite3','recovery.sqlite3.key'}
    assert not (tmp_path/'backup'/'secret.txt').exists()


def test_changed_backup_file_and_symlink_rejected(tmp_path):
    home,journal,point,result=save(tmp_path)
    key=tmp_path/'backup'/'recovery.sqlite3.key';key.write_bytes(b'x'*32)
    with pytest.raises(ValueError):b.restore(tmp_path/'backup',tmp_path/'restored',authoritative_checkpoint=point,expected_manifest_sha256=result['manifest_sha256'])
    alias=tmp_path/'alias';alias.symlink_to(home,target_is_directory=True)
    with pytest.raises(ValueError):b.snapshot(alias,tmp_path/'other','workspace',expected_checkpoint=point)


def test_hardlinked_source_key_rejected(tmp_path):
    home,journal=runtime(tmp_path);point=b.checkpoint(home,'workspace')
    os.link(home/'recovery.sqlite3.key',home/'alias-key')
    with pytest.raises(ValueError):b.snapshot(home,tmp_path/'backup','workspace',expected_checkpoint=point)


def test_coordinator_online_backup_preserves_unknown_429_and_inbox(tmp_path):
    from keel_muse.coordinator import demo,Coordinator
    home=tmp_path/'coordinator'
    assert demo(home)['status']=='PASS'
    before=Coordinator.open_readonly(home,'fixture').snapshot()
    authority=b.checkpoint(home,'fixture',profile='coordinator')
    saved=b.snapshot(home,tmp_path/'coordinator-backup','fixture',profile='coordinator',expected_checkpoint=authority)
    b.restore(tmp_path/'coordinator-backup',tmp_path/'coordinator-restored',authoritative_checkpoint=authority,expected_manifest_sha256=saved['manifest_sha256'])
    after=Coordinator.open_readonly(tmp_path/'coordinator-restored','fixture').snapshot()
    assert after==before
    assert after['state']['rate_limited'] is True
    assert after['state']['tasks']['second']['status']=='UNKNOWN'
