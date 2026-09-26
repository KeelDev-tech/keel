"""Deterministic retrieval measurements on explicitly labeled held-out queries."""
from .index import IndexError, _check
from keel_loki.common import clone, digest, require_id


def evaluate(index, dataset, *, expected_sha256, limit=5):
    dataset = clone(dataset)
    _check(digest(dataset) == expected_sha256, 'retrieval_dataset_changed')
    _check(type(dataset) is dict and set(dataset) == {'schema','split','synthetic','cases'}
           and dataset['schema'] == 'keel.memory.eval.v1' and dataset['split'] == 'held_out'
           and type(dataset['synthetic']) is bool, 'retrieval_dataset_invalid')
    cases = dataset['cases']
    _check(type(cases) is list and 1 <= len(cases) <= 256, 'retrieval_cases_invalid')
    results, seen, total_relevant, found_relevant, returned = [], set(), 0, 0, 0
    for case in cases:
        _check(type(case) is dict and set(case) == {'case_id','account_id','scope','purpose','query','relevant_documents'},
               'retrieval_case_invalid')
        require_id(case['case_id'])
        _check(case['case_id'] not in seen, 'retrieval_case_duplicate')
        seen.add(case['case_id'])
        labels = case['relevant_documents']
        _check(type(labels) is list and len(labels) <= 1024, 'retrieval_labels_invalid')
        for identity in labels:
            require_id(identity)
        _check(len(set(labels)) == len(labels), 'retrieval_labels_duplicate')
        report = index.search(case['query'], account_id=case['account_id'], scope=case['scope'],
                              purpose=case['purpose'], limit=limit)
        predictions = {m['document_id'] for m in report['matches']}
        hits = len(predictions & set(labels))
        found_relevant += hits; total_relevant += len(labels); returned += len(predictions)
        results.append({'case_id':case['case_id'], 'status':report['status'],
                        'relevant_count':len(labels),'returned_count':len(predictions),'relevant_returned':hits})
    return {'schema':'keel.memory.eval-report.v1','dataset_sha256':expected_sha256,
            'synthetic':dataset['synthetic'], 'labels_independently_authenticated':False,
            'recall_at_k':found_relevant/total_relevant if total_relevant else None,
            'precision_at_k':found_relevant/returned if returned else None,
            'k':limit,'cases':results,'execution_authorized':False}
