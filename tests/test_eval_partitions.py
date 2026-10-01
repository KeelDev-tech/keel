"""Exact split-overlap checks on existing synthetic evaluation datasets."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from keel_eval import EvaluationError
from keel_eval.__main__ import main
from keel_eval.partitions import audit_partitions


FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures/grounding_eval/dataset.json'


def dataset(partition, *, distinct=False):
    value = json.loads(FIXTURE.read_text())
    value['dataset_id'] = 'synthetic-' + partition
    value['split'] = 'held_out' if partition == 'held_out' else 'development'
    value['cases'] = value['cases'][:1]
    value['cases'][0]['case_id'] = partition + '-case'
    if distinct:
        value['cases'][0]['subject']['claims'][0]['text'] += ' Distinct synthetic subject.'
    return value


@pytest.mark.parametrize('other', ['demo', 'held_out'])
def test_renamed_case_ids_and_changed_labels_do_not_hide_exact_subjects(other):
    partitions = {'development': dataset('development'), other: dataset(other)}
    partitions[other]['cases'][0]['expected_verdict'] = 'FAIL'
    partitions[other]['cases'][0]['label_rationale'] = 'Different synthetic annotation.'
    before = deepcopy(partitions)
    report = audit_partitions(partitions)
    assert report['status'] == 'BLOCKED_EXACT_SUBJECT_OVERLAP'
    assert report['compared_case_count'] == 2
    assert report['overlapping_subject_count'] == 1
    assert report['overlaps'][0]['members'] == [
        {'partition': 'development', 'case_index': 0}, {'partition': other, 'case_index': 0}]
    assert partitions == before
    serialized = json.dumps(report)
    assert 'Different synthetic annotation.' not in serialized
    assert partitions['development']['cases'][0]['subject']['claims'][0]['text'] not in serialized


def test_disjoint_subjects_do_not_claim_semantic_independence_or_complete_partition_coverage():
    partitions = {'development': dataset('development'), 'held_out': dataset('held_out', distinct=True)}
    # IDs are local declarations, not a global identity registry.
    partitions['held_out']['cases'][0]['case_id'] = partitions['development']['cases'][0]['case_id']
    report = audit_partitions(partitions)
    assert report['status'] == 'NO_EXACT_OVERLAP_IN_SUPPLIED_PARTITIONS'
    assert report['overlaps'] == [] and report['compared_case_count'] == 2
    assert report['missing_partitions'] == ['demo']
    assert report['case_id_scope'] == 'dataset_local_declarations; external_identity_unknown'
    for flag in ('semantic_leakage_checked', 'split_independence_verified',
                 'dataset_identity_authenticated', 'execution_authorized'):
        assert report[flag] is False
    assert report['model_calls_attempted'] == report['canonical_writes'] == 0


def test_all_members_and_within_partition_duplicates_are_counted():
    inputs = {name: dataset(name) for name in ('development', 'demo', 'held_out')}
    duplicate = deepcopy(inputs['development']['cases'][0])
    duplicate['case_id'] = 'another-case'
    inputs['development']['cases'].append(duplicate)
    report = audit_partitions(inputs)
    assert report['compared_case_count'] == 4
    assert len(report['overlaps'][0]['members']) == 4
    assert report['partitions']['development']['unique_subject_count'] == 1
    assert report['missing_partitions'] == []


def test_within_partition_duplicates_are_not_misreported_as_cross_partition_overlap():
    inputs = {'development': dataset('development'), 'held_out': dataset('held_out', distinct=True)}
    duplicate = deepcopy(inputs['development']['cases'][0])
    duplicate['case_id'] = 'another-case'
    inputs['development']['cases'].append(duplicate)
    report = audit_partitions(inputs)
    assert report['compared_case_count'] == 3 and report['overlaps'] == []
    assert report['partitions']['development']['case_count'] == 2
    assert report['partitions']['development']['unique_subject_count'] == 1


@pytest.mark.parametrize('change', ['missing_id', 'unknown_id_key', 'invalid_id', 'duplicate_id',
                                   'wrong_split', 'too_many_cases'])
def test_missing_or_invalid_identity_and_manifest_inputs_never_produce_a_clean_report(change):
    inputs = {'development': dataset('development'), 'held_out': dataset('held_out')}
    target = inputs['held_out']
    if change == 'missing_id':
        target['cases'][0].pop('case_id')
    elif change == 'unknown_id_key':
        target['cases'][0]['external_id'] = target['cases'][0].pop('case_id')
    elif change == 'invalid_id':
        target['cases'][0]['case_id'] = None
    elif change == 'duplicate_id':
        target['cases'].append(deepcopy(target['cases'][0]))
    elif change == 'wrong_split':
        target['split'] = 'development'
    else:
        target['cases'] *= 257
    with pytest.raises(EvaluationError):
        audit_partitions(inputs)


@pytest.mark.parametrize('names', [[], ['development'], ['development', 'unknown']])
def test_partition_names_and_minimum_comparison_scope_are_closed(names):
    with pytest.raises(EvaluationError):
        audit_partitions({name: dataset(name) for name in names})


def test_cli_is_read_only_offline_and_returns_nonzero_for_overlap(tmp_path, capsys, monkeypatch):
    import keel_eval.__main__ as cli
    monkeypatch.setattr(cli, 'run_local', lambda *_a, **_k: pytest.fail('model dispatch'))
    paths = {name: tmp_path / (name + '.json') for name in ('development', 'held_out')}
    for name, path in paths.items():
        path.write_text(json.dumps(dataset(name)))
    before = {path: path.read_bytes() for path in paths.values()}
    args = ['partition-audit', '--development', str(paths['development']), '--held-out', str(paths['held_out'])]
    assert main(args) == 3
    assert json.loads(capsys.readouterr().out)['overlapping_subject_count'] == 1
    assert before == {path: path.read_bytes() for path in paths.values()}
    paths['held_out'].write_text(json.dumps(dataset('held_out', distinct=True)))
    assert main(args) == 0
    capsys.readouterr()
    invalid = dataset('held_out'); invalid['cases'][0].pop('case_id')
    paths['held_out'].write_text(json.dumps(invalid))
    assert main(args) == 2
    assert not capsys.readouterr().out


def test_source_package_runs_partition_audit_without_installed_packages(tmp_path):
    import os
    import subprocess
    import sys
    import zipfile
    from tools import package

    archive = tmp_path / 'source.zip'
    package.build(archive)
    code = tmp_path / 'source'
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(code)
    paths = {}
    for name in ('development', 'held_out'):
        paths[name] = tmp_path / (name + '.json')
        paths[name].write_text(json.dumps(dataset(name)))
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, '-S', '-m', 'keel_eval', 'partition-audit',
        '--development', str(paths['development']), '--held-out', str(paths['held_out'])],
        cwd=code, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 3, result.stdout + result.stderr
    assert json.loads(result.stdout)['overlapping_subject_count'] == 1
