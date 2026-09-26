"""Closed, bounded artifact interpreter; no executable names or callbacks in data."""
from keel_machine.common import canonical, digest, require


def validate_rule(rule):
    require(type(rule) is dict and set(rule) == {'path', 'equals', 'on_match', 'otherwise'}, 'artifact_rule_invalid')
    require(type(rule['path']) is list and 1 <= len(rule['path']) <= 8
            and all(type(x) is str and 0 < len(x) <= 64 for x in rule['path']), 'artifact_rule_path_invalid')
    require(rule['on_match'] in ('PASS', 'FAIL', 'ABSTAIN')
            and rule['otherwise'] in ('PASS', 'FAIL', 'ABSTAIN'), 'artifact_rule_verdict_invalid')
    canonical(rule, 8192)
    return rule


def artifact_runner(subject, config, seed):
    """The pinned candidate reads and interprets the complete artifact on every trial."""
    require(type(config) is dict and set(config) == {'artifact', 'artifact_body_sha256'}, 'artifact_runner_config_invalid')
    body = config['artifact']
    require(digest(body) == config['artifact_body_sha256'], 'artifact_runner_hash_mismatch')
    if 'steps' in body:
        require(body['steps'] == [{'operation': 'evaluate_rule'}], 'unsupported_procedure_interpreter')
    rule = validate_rule(body.get('rule'))
    value = subject
    for part in rule['path']:
        if type(value) is not dict or part not in value:
            return 'ABSTAIN'
        value = value[part]
    return rule['on_match'] if canonical(value) == canonical(rule['equals']) else rule['otherwise']


def baseline_runner(subject, config, seed):
    require(type(config) is dict and set(config) == {'verdict'}
            and config['verdict'] in ('PASS', 'FAIL', 'ABSTAIN'), 'baseline_config_invalid')
    return config['verdict']
