"""Detect exact evaluation-set leakage without claiming semantic independence."""
from collections import defaultdict
from keel_eval.evaluation import validate_dataset,dataset_digest
from keel_trust.common import digest
from .experiment import BenchmarkError


def audit_partition(development, heldout):
    development,heldout=validate_dataset(development),validate_dataset(heldout)
    if development['split']!='development' or heldout['split']!='held_out':
        raise BenchmarkError('dataset_partition_roles_invalid')
    def index(dataset):
        subjects,evidence=defaultdict(list),defaultdict(list)
        for case in dataset['cases']:
            # Strip arbitrary IDs/order, retaining the actual model-visible prose.
            subject=digest({'claims':sorted(c['text'] for c in case['subject']['claims']),
                            'evidence':sorted(e['text'] for e in case['subject']['evidence'])})
            subjects[subject].append(case['case_id'])
            for item in case['subject']['evidence']:
                evidence[digest(item['text'])].append(case['case_id'])
        return subjects,evidence
    ds,de=index(development);hs,he=index(heldout)
    overlapping_subjects=[{'content_sha256':key,'development_case_ids':ds[key],'heldout_case_ids':hs[key]}
                          for key in sorted(ds.keys()&hs.keys())]
    overlapping_evidence=[{'content_sha256':key,'development_case_ids':sorted(set(de[key])),
                          'heldout_case_ids':sorted(set(he[key]))} for key in sorted(de.keys()&he.keys())]
    duplicates=[{'content_sha256':key,'heldout_case_ids':ids} for key,ids in sorted(hs.items()) if len(ids)>1]
    labels={gold:sum(c['expected_verdict']==gold for c in heldout['cases']) for gold in ('PASS','FAIL','ABSTAIN')}
    overlap=bool(overlapping_subjects or overlapping_evidence or duplicates)
    return {'schema':'keel.bench.partition-audit.v1','status':'EXACT_OVERLAP_DETECTED' if overlap else 'NO_EXACT_OVERLAP_DETECTED',
        'development_sha256':dataset_digest(development),'heldout_sha256':dataset_digest(heldout),
        'subject_overlap':overlapping_subjects,'evidence_overlap':overlapping_evidence,'duplicate_heldout_subjects':duplicates,
        'heldout_label_counts':labels,'missing_label_classes':[k for k,v in labels.items() if not v],
        'semantic_overlap_checked':False,'training_contamination_checked':False,
        'heldout_independence_verified':False,'labels_independently_verified':False,
        'execution_authorized':False,
        'boundary':'Exact text-content overlap only; changed IDs cannot hide identical content. '
                   'Paraphrases, shared underlying people/documents, training overlap and label correctness need independent review.'}
