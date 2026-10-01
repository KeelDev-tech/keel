"""Offline drift checks for the two test workflows, not a security boundary.

Keep current explicit read-only permissions, ubuntu-latest runners and absence
of secret-context references. CodeQL has a separate permission contract.
This does not inspect actions, scripts, repository defaults or branch rules.
"""
from pathlib import Path
import re
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ('ci.yml', 'recovery-profile.yml')


class WorkflowLoader(yaml.BaseLoader):
    # BaseLoader preserves GitHub's `on` key instead of YAML 1.1 boolean `True`.
    # Reject ambiguous duplicate keys; never instantiate arbitrary YAML objects.
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result or key == '<<':
                raise ValueError('duplicate, complex or merged workflow key')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def readonly(value):
    return value == 'read-all' or (type(value) is dict and
        all(level in ('read', 'none') for level in value.values()))


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from strings(key)
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def check_policy(document):
    assert type(document) is dict, 'workflow must be a mapping'
    assert 'permissions' in document and readonly(document['permissions']), 'explicit read-only workflow permissions required'
    jobs = document.get('jobs')
    assert type(jobs) is dict and jobs, 'jobs required'
    for job in jobs.values():
        assert type(job) is dict, 'job must be a mapping'
        assert readonly(job.get('permissions', document['permissions'])), 'job permissions must remain read-only'
        # Freeze these workflows' current literal runner; expressions, groups
        # and reusable workflows require deliberate review of this contract.
        assert job.get('runs-on') == 'ubuntu-latest', 'current hosted runner required'
        assert not job.get('secrets'), 'job secret forwarding forbidden'
    for text in strings(document):
        for expression in re.findall(r"\$\{\{((?:'(?:[^']|'')*'|[^'}]|}(?!}))*?)\}\}", text, flags=re.S):
            # GitHub expressions use single-quoted strings with doubled quotes.
            code = re.sub(r"'(?:[^']|'')*'", '', expression)
            assert not re.search(r'\bsecrets\b', code, flags=re.I), 'secret context reference forbidden'


class WorkflowPolicyTests(unittest.TestCase):
    def fixture(self):
        return yaml.load('''on: [push, pull_request]
permissions: {contents: read}
jobs:
  test:
    runs-on: ubuntu-latest
    steps: [{run: "python -m unittest discover -s tests"}]
''', Loader=WorkflowLoader)

    def test_actual_test_workflows(self):
        for name in WORKFLOWS:
            with self.subTest(workflow=name):
                check_policy(yaml.load((ROOT/'.github/workflows'/name).read_text(), Loader=WorkflowLoader))

    def test_explicit_readonly_forms_and_job_inheritance(self):
        for permissions in ({}, {'contents': 'read'}, {'contents': 'none'}, 'read-all'):
            with self.subTest(permissions=permissions):
                doc = self.fixture(); doc['permissions'] = permissions
                check_policy(doc)
                doc['jobs']['test']['permissions'] = {'contents': 'read'}
                check_policy(doc)
        self.assertIn('on', self.fixture())

    def test_implicit_write_and_dynamic_permissions_are_rejected(self):
        for level in ('workflow', 'job'):
            for permissions in (None, '', 'write-all', {'contents':'write'}, {'id-token':'write'}, '${{ inputs.permissions }}'):
                with self.subTest(level=level, permissions=permissions):
                    doc = self.fixture(); target = doc if level == 'workflow' else doc['jobs']['test']
                    target['permissions'] = permissions
                    with self.assertRaises(AssertionError): check_policy(doc)
        doc = self.fixture(); del doc['permissions']
        with self.assertRaises(AssertionError): check_policy(doc)

    def test_nonliteral_or_noncurrent_runners_are_rejected(self):
        for runner in (None, 'self-hosted', ['self-hosted', 'linux'], {'group':'private'}, '${{ matrix.os }}'):
            with self.subTest(runner=runner):
                doc = self.fixture(); doc['jobs']['test']['runs-on'] = runner
                with self.assertRaises(AssertionError): check_policy(doc)

    def test_secret_references_are_rejected_at_nested_locations(self):
        for reference in ('${{ secrets.TOKEN }}', "${{ secrets['TOKEN'] }}", '${{ toJSON(secrets) }}',
                          '${{ SeCrEtS.TOKEN }}', "${{ contains('}}', secrets.TOKEN) }}"):
            for location in ('env', 'with', 'run'):
                with self.subTest(reference=reference, location=location):
                    doc = self.fixture(); doc['jobs']['test']['steps'][0][location] = {'TOKEN':reference} if location != 'run' else reference
                    with self.assertRaises(AssertionError): check_policy(doc)
        doc = self.fixture(); doc['jobs']['test']['secrets'] = 'inherit'
        with self.assertRaises(AssertionError): check_policy(doc)

    def test_comments_literals_and_unrelated_expressions_do_not_trigger(self):
        doc = self.fixture()
        doc['jobs']['test']['steps'][0]['run'] = "python -c 'import secrets; secrets.token_hex()'"
        doc['env'] = {'LITERAL': "${{ 'secrets.TOKEN' }}", 'VERSION':'${{ matrix.python-version }}'}
        check_policy(doc)
        check_policy(yaml.load(yaml.safe_dump(doc)+ '\n# secrets.TOKEN permissions: write-all\n', Loader=WorkflowLoader))

    def test_duplicate_keys_and_yaml_merges_are_rejected(self):
        for text in ('permissions: {}\npermissions: write-all', 'base: &base {contents: read}\npermissions: {<<: *base}'):
            with self.assertRaises(ValueError): yaml.load(text, Loader=WorkflowLoader)

    def test_only_test_workflows_are_in_scope(self):
        self.assertEqual(WORKFLOWS, ('ci.yml', 'recovery-profile.yml'))


if __name__ == '__main__':
    unittest.main()
