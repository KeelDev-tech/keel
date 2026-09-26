"""Additive installation integrity; no existing file is replaced."""
import hashlib
import json
from pathlib import Path

import pytest

from tools.install_source_producers import inspect, install


def setup_patch(tmp_path):
    target, patch = tmp_path/'target', tmp_path/'patch'
    for root in (target, patch):
        root.mkdir()
    (target/'keel_agent').mkdir()
    (target/'keel_agent/base.py').write_text('BASE SOURCE\n')
    (patch/'keel_sources').mkdir()
    (patch/'keel_sources/new.py').write_text('ADDITIVE SOURCE\n')
    digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    manifest={'schema':'keel.additive_patch.v1','version':'0.8.0-review.1',
              'base_files':{'keel_agent/base.py':digest(target/'keel_agent/base.py')},
              'files':{'keel_sources/new.py':digest(patch/'keel_sources/new.py')}}
    (patch/'keel_sources/PATCH_MANIFEST.json').write_text(json.dumps(manifest))
    return patch,target


def test_verified_addition_and_idempotent_repeat_preserve_base(tmp_path):
    patch,target=setup_patch(tmp_path)
    before=(target/'keel_agent/base.py').read_bytes()
    assert install(patch,target)['status']=='VERIFIED_FOR_ADDITION'
    assert not (target/'keel_sources').exists()
    assert install(patch,target,write=True)['new_files']==2
    assert install(patch,target,write=True)['new_files']==0
    assert (target/'keel_agent/base.py').read_bytes()==before


def test_base_mismatch_prevents_all_writes(tmp_path):
    patch,target=setup_patch(tmp_path)
    (target/'keel_agent/base.py').write_text('DIFFERENT BASE')
    with pytest.raises(ValueError,match='base dependency'):
        install(patch,target,write=True)
    assert not (target/'keel_sources').exists()


def test_existing_different_file_never_overwritten(tmp_path):
    patch,target=setup_patch(tmp_path)
    (target/'keel_sources').mkdir()
    (target/'keel_sources/new.py').write_text('EXISTING WORK')
    with pytest.raises(ValueError,match='existing file conflict'):
        install(patch,target,write=True)
    assert (target/'keel_sources/new.py').read_text()=='EXISTING WORK'


def test_corrupt_patch_and_symlink_refused(tmp_path):
    patch,target=setup_patch(tmp_path)
    (patch/'keel_sources/new.py').write_text('MODIFIED')
    with pytest.raises(ValueError,match='patch digest mismatch'):
        install(patch,target,write=True)
    (target/'keel_agent/base.py').rename(target/'elsewhere')
    (target/'keel_agent/base.py').symlink_to(target/'elsewhere')
    with pytest.raises(ValueError,match='symlink'):
        inspect(patch,target)


def test_failed_write_removes_only_files_created_this_time(tmp_path,monkeypatch):
    patch,target=setup_patch(tmp_path)
    def failing_fsync(_):
        raise OSError('synthetic disk failure')
    monkeypatch.setattr('tools.install_source_producers.os.fsync',failing_fsync)
    with pytest.raises(OSError):
        install(patch,target,write=True)
    assert not (target/'keel_sources/new.py').exists()
    assert (target/'keel_agent/base.py').read_text()=='BASE SOURCE\n'
