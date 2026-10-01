"""Read-only exact subject comparison; no semantic independence claim."""
from collections import defaultdict

from .evaluation import EvaluationError, _sha, validate_dataset


PARTITIONS = ('development', 'demo', 'held_out')


def audit_partitions(partitions):
    """Compare two or three existing v1 datasets without changing their format.

    Demo is an audit role for a development-split dataset. Case IDs remain
    dataset-local declarations: renaming one cannot hide identical subjects,
    and matching IDs alone cannot establish identity across different inputs.
    """
    if (type(partitions) is not dict or not 2 <= len(partitions) <= len(PARTITIONS)
            or not set(partitions) <= set(PARTITIONS)):
        raise EvaluationError('partition_audit_scope_invalid')
    summaries, subjects = {}, defaultdict(list)
    for partition in PARTITIONS:
        if partition not in partitions:
            continue
        # Includes bounded structure/bytes, required case IDs and uniqueness.
        # Invalid inputs abort the whole audit; no cases silently disappear.
        dataset = validate_dataset(partitions[partition])
        expected_split = 'held_out' if partition == 'held_out' else 'development'
        if dataset['split'] != expected_split:
            raise EvaluationError('partition_dataset_split_mismatch')
        fingerprints = set()
        for index, case in enumerate(dataset['cases']):
            fingerprint = _sha(case['subject'])
            fingerprints.add(fingerprint)
            subjects[fingerprint].append({'partition': partition, 'case_index': index})
        summaries[partition] = {'dataset_sha256': _sha(dataset),
            'case_count': len(dataset['cases']), 'unique_subject_count': len(fingerprints),
            'declared_split': dataset['split'], 'synthetic': dataset['synthetic']}
    overlaps = [{'subject_sha256': fingerprint, 'members': members}
                for fingerprint, members in sorted(subjects.items())
                if len({member['partition'] for member in members}) > 1]
    return {'schema': 'keel.eval.partition-audit.v1',
        'status': 'BLOCKED_EXACT_SUBJECT_OVERLAP' if overlaps else 'NO_EXACT_OVERLAP_IN_SUPPLIED_PARTITIONS',
        'partitions': summaries,
        'missing_partitions': [name for name in PARTITIONS if name not in summaries],
        'compared_case_count': sum(row['case_count'] for row in summaries.values()),
        'overlapping_subject_count': len(overlaps), 'overlaps': overlaps,
        'fingerprint_scope': 'canonical_json_subject; exact_strings_and_array_order',
        'case_id_scope': 'dataset_local_declarations; external_identity_unknown',
        'case_id_validation': 'required_and_unique_within_each_dataset',
        'semantic_leakage_checked': False, 'split_independence_verified': False,
        'dataset_identity_authenticated': False, 'quality_release_gate_satisfied': False,
        'execution_authorized': False, 'model_calls_attempted': 0, 'canonical_writes': 0}
