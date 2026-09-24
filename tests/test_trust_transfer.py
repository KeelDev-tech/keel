"""Receiver failure modes are checked before any destination is created."""
import base64
import hashlib
import io
import json
import lzma
from pathlib import Path
import runpy
import sys
import pytest
from tools.transfer_reader import decode_transfer,extract
from tools.single_file_transfer import build


def encoded_files(names,**kw):
    raw=io.BytesIO();raw.write(b'KEEL_TEXT_BUNDLE_V1\n');raw.write((json.dumps({'files':len(names)})+'\n').encode())
    for name in names:
        body=b'fixture\n';entry={'path':name,'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()}
        entry.update(kw)
        raw.write(b'FILE '+json.dumps(entry).encode()+b'\n'+body+b'\n')
    raw.write(b'END\n');body=raw.getvalue()
    return [base64.b64encode(lzma.compress(body)).decode(),len(body),hashlib.sha256(body).hexdigest(),len(names)]


@pytest.mark.parametrize('names',[['../outside'],['/absolute'],['a/../b'],['a\\b'],['a:b'],['a','a'],['A','a'],
    ['folder','folder/file.py'],['Folder','folder/file.py'],['a\nfile'],['./a'],['']])
def test_transfer_rejects_unsafe_or_conflicting_paths(names):
    with pytest.raises(ValueError):decode_transfer(*encoded_files(names))


@pytest.mark.parametrize('change',['truncated','digest','size','count','trailing','bad_file_hash','bad_file_size'])
def test_transfer_rejects_corrupt_payload(change):
    args=encoded_files(['README.md'])
    if change=='truncated':args[0]=args[0][:-8]
    if change=='digest':args[2]='0'*64
    if change=='size':args[1]-=1
    if change=='count':args[3]+=1
    if change=='trailing':args[0]=base64.b64encode(base64.b64decode(args[0])+b'extra').decode()
    if change=='bad_file_hash':args=encoded_files(['README.md'],sha256='0'*64)
    if change=='bad_file_size':args=encoded_files(['README.md'],bytes=True)
    with pytest.raises(ValueError):decode_transfer(*args)


@pytest.mark.parametrize('kind',['directory','file','symlink','broken_symlink'])
def test_transfer_never_overwrites_destination(kind,tmp_path):
    path=tmp_path/'destination'
    if kind=='directory':path.mkdir()
    if kind=='file':path.write_text('existing')
    if kind=='symlink':path.symlink_to(tmp_path,target_is_directory=True)
    if kind=='broken_symlink':path.symlink_to(tmp_path/'missing')
    files=decode_transfer(*encoded_files(['README.md']))
    with pytest.raises(FileExistsError):extract(files,path)


def test_single_file_contains_everything_and_never_executes_bundled_code(tmp_path,capsys):
    root=tmp_path/'source';root.mkdir()
    (root/'VERSION').write_text('synthetic-test')
    (root/'danger.py').write_text("raise RuntimeError('bundled code must never execute during extraction')\n")
    (root/'release-files.json').write_text(json.dumps(['VERSION','danger.py']))
    output=tmp_path/'transfer.txt';receipt=build(output,root)
    assert receipt['files']==2
    # Load our generated trusted extractor in-process; keep the no-arbitrary-child guard intact.
    receiver=runpy.run_path(str(output),run_name='keel_transfer_fixture')
    assert receiver['main'](['--verify-only'])==0
    assert json.loads(capsys.readouterr().out)['verified_integrity']
    destination=tmp_path/'restored'
    assert receiver['main'](['--out',str(destination)])==0
    assert (destination/'danger.py').read_bytes()==(root/'danger.py').read_bytes()
    old=output.read_bytes()
    with pytest.raises(FileExistsError):build(output,root)
    assert output.read_bytes()==old
