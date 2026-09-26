"""Bounded JSON and private SQLite storage for trusted local machine services.

Same-UID processes and the host administrator are trusted. This storage checks
permissions and identities; it is not a sandbox or a tamper-proof remote ledger.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat


class MachineError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise MachineError(code)


def ident(value):
    require(type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value),
            'identifier_invalid')
    return value


def sha(value):
    require(type(value) is str and re.fullmatch(r'[a-f0-9]{64}', value), 'sha256_invalid')
    return value


def canonical(value, limit=262144):
    require(type(limit) is int and 1 <= limit <= 16777216, 'json_limit_invalid')
    pending=[(value, 0)]; seen=0; characters=0
    while pending:
        item,depth=pending.pop(); seen+=1
        require(depth <= 32 and seen <= 65536, 'json_structure_too_large')
        kind=type(item)
        if kind is dict:
            require(len(item)<=4096 and all(type(k) is str for k in item), 'json_object_invalid')
            for key in item:
                require(len(key)<=min(limit,4096),'json_key_too_large')
                characters+=len(key)
                require(characters<=limit,'json_text_too_large')
            pending.extend((child,depth+1) for child in item.values())
        elif kind is list:
            require(len(item)<=16384, 'json_array_too_large')
            pending.extend((child,depth+1) for child in item)
        elif kind is float:
            require(math.isfinite(item), 'json_number_not_finite')
        elif kind is int:
            require(abs(item)<=2**63-1, 'json_integer_out_of_range')
        else:
            require(kind in (str,bool,type(None)), 'json_type_invalid')
        if kind is str:
            require(len(item)<=limit, 'json_string_too_large')
            characters+=len(item)
            require(characters<=limit,'json_text_too_large')
    try:
        body=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
    except (ValueError,TypeError,UnicodeError,RecursionError):
        raise MachineError('json_encoding_invalid') from None
    require(len(body)<=limit, 'json_bytes_too_large')
    return body


def clone(value, limit=262144):
    return json.loads(canonical(value,limit))


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _ancestors(path):
    for parent in path.parents:
        info=parent.lstat()
        require(stat.S_ISDIR(info.st_mode), 'storage_symlink_ancestor')
        require(info.st_uid in (0,os.getuid()), 'storage_untrusted_ancestor')
        require(not info.st_mode & 0o022 or info.st_mode & stat.S_ISVTX,
                'storage_writable_ancestor')


def _private(path, directory=False):
    info=path.lstat()
    require(path.resolve(strict=True)==path, 'storage_symlink')
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
            'storage_wrong_type')
    require(stat.S_IMODE(info.st_mode)==(0o700 if directory else 0o600), 'storage_not_private')
    require(info.st_uid==os.getuid(), 'storage_wrong_owner')
    require(directory or info.st_nlink==1, 'storage_hardlink')
    return info.st_dev,info.st_ino


class PrivateDB:
    """One private SQLite file; its parent must already exist with mode 0700."""
    def __init__(self,path):
        self.path=Path(os.path.abspath(path))
        _ancestors(self.path)
        self.parent_identity=_private(self.path.parent,True)
        self.created=False
        try:
            descriptor=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
            os.close(descriptor)
            self.created=True
        except FileExistsError:
            pass
        self.identity=_private(self.path)

    def _check(self):
        _ancestors(self.path)
        require(_private(self.path.parent,True)==self.parent_identity
                and _private(self.path)==self.identity,'storage_replaced')
        for suffix in ('-wal','-shm','-journal'):
            companion=Path(str(self.path)+suffix)
            if companion.exists() or companion.is_symlink():
                _private(companion)

    @contextmanager
    def transaction(self):
        self._check()
        db=None
        try:
            db=sqlite3.connect(self.path.as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=10)
            db.row_factory=sqlite3.Row
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('PRAGMA temp_store=MEMORY')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            yield db
            self._check()
            db.commit()
        except sqlite3.Error:
            if db is not None and db.in_transaction:
                db.rollback()
            raise MachineError('storage_database_error') from None
        except BaseException:
            if db is not None and db.in_transaction:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()
