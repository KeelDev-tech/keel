import pytest
from keel_loki.common import atomic_json,load_json,decode_json,digest,LokiError


def test_exclusive_publish_and_no_symlink(tmp_path):
    p=tmp_path/'result.json'
    atomic_json(p,{'state':'original'})
    with pytest.raises(FileExistsError):atomic_json(p,{'state':'changed'})
    assert load_json(p)=={'state':'original'}
    link=tmp_path/'link.json';link.symlink_to(p)
    with pytest.raises((OSError,ValueError)):load_json(link)
    assert list(tmp_path.glob('.keel-loki-*'))==[]


@pytest.mark.parametrize('raw',['{"x":1,"x":2}','{"x":NaN}','{"x":Infinity}'])
def test_ambiguous_json_rejected(raw):
    with pytest.raises(LokiError):decode_json(raw)


def test_type_distinct_pins():
    assert digest(True)!=digest(1)!=digest(1.0)
