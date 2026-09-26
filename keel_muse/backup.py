"""Explicit runtime-store snapshots and conservative new-directory restore.

Only the named recovery/coordinator SQLite stores and their matching host keys
are supported. This is not a full Keel backup. An externally retained current
checkpoint is mandatory; a backup cannot prove that newer state does not exist.
"""
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import time

from keel_loki.common import clone,digest,require_dict,require_hash,require_id,atomic_json,load_json
from tools.bench_inventory import directory_fd,read_file

PROFILES={'recovery':'recovery.sqlite3','coordinator':'coordinator.sqlite3'}


def _reader(profile,home,workspace_id):
    if profile=='recovery':
        from keel_loki.recovery import RecoveryJournal
        return RecoveryJournal.open_readonly(home,workspace_id)
    if profile=='coordinator':
        from keel_muse.coordinator import Coordinator
        return Coordinator.open_readonly(home,workspace_id)
    raise ValueError('backup_profile_not_allowed')


def checkpoint(home,workspace_id,*,profile='recovery'):
    require_id(workspace_id)
    snapshot=_reader(profile,home,workspace_id).snapshot()
    return {'schema':'keel.muse.backup-checkpoint.v1','profile':profile,'workspace_id':workspace_id,
            'checkpoint_sha256':snapshot['checkpoint_sha256'],'event_head_sha256':snapshot['event_head_sha256']}


def _checkpoint(value):
    value=clone(value)
    require_dict(value,{'schema','profile','workspace_id','checkpoint_sha256','event_head_sha256'})
    if value['schema']!='keel.muse.backup-checkpoint.v1' or value['profile'] not in PROFILES: raise ValueError('backup_checkpoint_invalid')
    require_id(value['workspace_id']);require_hash(value['checkpoint_sha256']);require_hash(value['event_head_sha256'])
    return value


def _new_directory(path):
    path=Path(path).absolute()
    parent=directory_fd(path.parent)
    try:
        os.mkdir(Path('/proc/self/fd')/str(parent)/path.name,mode=0o700)
        descriptor=os.open(path.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
        return path,descriptor
    finally: os.close(parent)


def _write(descriptor,name,body):
    target=Path('/proc/self/fd')/str(descriptor)/name
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write(body);stream.flush();os.fsync(stream.fileno())


def snapshot(runtime_home,backup_home,workspace_id,*,expected_checkpoint,profile='recovery'):
    """SQLite online backup with pinned host checkpoint before and after copy."""
    expected_checkpoint=_checkpoint(expected_checkpoint)
    if expected_checkpoint['profile']!=profile or expected_checkpoint['workspace_id']!=workspace_id: raise ValueError('backup_scope_mismatch')
    home=Path(runtime_home).absolute(); source=directory_fd(home)
    destination=None
    try:
        if checkpoint(home,workspace_id,profile=profile)!=expected_checkpoint: raise ValueError('backup_checkpoint_mismatch')
        name=PROFILES[profile]; key=read_file(home,name+'.key')
        if len(key)!=32: raise ValueError('backup_key_invalid')
        filefd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=source)
        try:
            before=os.fstat(filefd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>8*1024*1024: raise ValueError('backup_database_invalid')
            path,destination=_new_directory(backup_home)
            _write(destination,name+'.key',key)
            anchored_source=Path('/proc/self/fd')/str(source)/name
            # The source directory remains anchored, including WAL lookup.
            # SQLite writes only into memory. Publication uses an exclusive
            # descriptor-safe file write, never SQLite following a target link.
            started=time.monotonic()
            with closing(sqlite3.connect('file:'+str(anchored_source)+'?mode=ro',uri=True,timeout=5)) as src:
                page_size=src.execute('PRAGMA page_size').fetchone()[0]
                def progress(status,remaining,total):
                    if status in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED): raise ValueError('backup_source_busy')
                    if time.monotonic()-started>30 or total*page_size>8*1024*1024: raise ValueError('backup_copy_bound_exceeded')
                with closing(sqlite3.connect(':memory:')) as dst:
                    src.backup(dst,pages=64,progress=progress,sleep=0)
                    body=dst.serialize()
            if len(body)>8*1024*1024: raise ValueError('backup_copy_bound_exceeded')
            _write(destination,name,body)
            after=os.stat(name,dir_fd=source,follow_symlinks=False)
            if (before.st_dev,before.st_ino,before.st_nlink)!=(after.st_dev,after.st_ino,after.st_nlink): raise ValueError('backup_source_replaced')
            if read_file(home,name+'.key')!=key or checkpoint(home,workspace_id,profile=profile)!=expected_checkpoint: raise ValueError('backup_source_changed')
            if checkpoint(path,workspace_id,profile=profile)!=expected_checkpoint: raise ValueError('backup_copy_checkpoint_mismatch')
            files={n:hashlib.sha256(read_file(path,n)).hexdigest() for n in (name,name+'.key')}
            manifest={'schema':'keel.muse.backup.v1','profile':profile,'workspace_id':workspace_id,
                'checkpoint':expected_checkpoint,'files':files,'contains_host_key':True,'full_keel_backup':False,'execution_authorized':False}
            atomic_json(path/'backup-manifest.json',manifest)
            os.fsync(destination)
            return {'manifest':manifest,'manifest_sha256':digest(manifest),'backup_home':str(path),'execution_authorized':False}
        finally: os.close(filefd)
    finally:
        if destination is not None: os.close(destination)
        os.close(source)


def restore(backup_home,destination,*,authoritative_checkpoint,expected_manifest_sha256):
    """Restore only the exact authoritative current head into a new directory.

    The authoritative checkpoint must come from a trusted current host or an
    independently retained latest record, not merely from this backup. An old,
    absent or unknown checkpoint cannot clear UNKNOWN, 429 or revoked approval.
    """
    authority=_checkpoint(authoritative_checkpoint);require_hash(expected_manifest_sha256)
    backup=Path(backup_home).absolute(); manifest=load_json(backup/'backup-manifest.json')
    require_dict(manifest,{'schema','profile','workspace_id','checkpoint','files','contains_host_key','full_keel_backup','execution_authorized'})
    if manifest['schema']!='keel.muse.backup.v1' or digest(manifest)!=expected_manifest_sha256: raise ValueError('backup_manifest_pin_mismatch')
    if manifest['checkpoint']!=authority or manifest['profile']!=authority['profile'] or manifest['workspace_id']!=authority['workspace_id']: raise ValueError('restore_authoritative_checkpoint_mismatch')
    if manifest['contains_host_key'] is not True or manifest['full_keel_backup'] is not False or manifest['execution_authorized'] is not False: raise ValueError('backup_boundary_invalid')
    profile=manifest['profile']; name=PROFILES[profile]
    if type(manifest['files']) is not dict or set(manifest['files'])!={name,name+'.key'}: raise ValueError('backup_file_allowlist_invalid')
    captured={n:read_file(backup,n) for n in manifest['files']}
    for name_,raw in captured.items():
        if hashlib.sha256(raw).hexdigest()!=manifest['files'][name_]: raise ValueError('backup_file_hash_mismatch')
    if checkpoint(backup,authority['workspace_id'],profile=profile)!=authority: raise ValueError('backup_authenticated_state_mismatch')
    path,descriptor=_new_directory(destination)
    try:
        for name_,raw in captured.items(): _write(descriptor,name_,raw)
        if checkpoint(path,authority['workspace_id'],profile=profile)!=authority: raise ValueError('restore_state_mismatch')
        receipt={'schema':'keel.muse.restore.v1','manifest_sha256':expected_manifest_sha256,'checkpoint':authority,
                 'restored_profile':profile,'destination':str(path),'controller_started':False,
                 'checkpoint_authority_authenticated':False,'execution_authorized':False}
        atomic_json(path/'restore-receipt.json',receipt)
        os.fsync(descriptor)
        return receipt
    finally: os.close(descriptor)


def demo(home):
    from keel_loki.recovery import RecoveryJournal
    home=Path(home).absolute();home.mkdir(mode=0o700,parents=True,exist_ok=True)
    runtime=home/'runtime';runtime.mkdir(mode=0o700)
    journal=RecoveryJournal(runtime,'fixture',now=100)
    journal.record_429(now=101)
    current=checkpoint(runtime,'fixture')
    saved=snapshot(runtime,home/'backup','fixture',expected_checkpoint=current)
    restored=restore(home/'backup',home/'restored',authoritative_checkpoint=current,expected_manifest_sha256=saved['manifest_sha256'])
    state=RecoveryJournal.open_readonly(home/'restored','fixture').snapshot()['state']
    return {'backup':saved,'restore':restored,'rate_limit_preserved':state['rate_limited'],'execution_authorized':False}
