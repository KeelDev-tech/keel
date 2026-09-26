#!/usr/bin/env python3
"""Synthetic actual-file rehearsal; no inference, network, browser or dispatch."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from keel_grounding.demo import make_fixture
from keel_grounding.claims import verify_grounding
from keel_grounding.answers import resolve_grounded_answer
from keel_grounding.packet import verify_packet
from keel_grounding.evidence import GroundingError
from keel_eval.evaluation import evaluate_replay
from keel_trust.common import digest
from tools.grounding_inventory import inventory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    out = Path(args.out).absolute(); out.mkdir(parents=True, exist_ok=False, mode=0o700)
    before = inventory()
    events = []
    def guard(event, args):
        if event in {'socket.__new__', 'socket.connect', 'subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn'}:
            events.append(event)
            raise RuntimeError('synthetic acceptance forbids network and process execution')
    sys.addaudithook(guard)
    f = make_fixture(out/'synthetic')
    eroot, proot = out/'synthetic/evidence', out/'synthetic/packet'
    def evidence(current=f):
        return verify_grounding(current['document'], current['bindings'], root=eroot, now=current['now'])
    def answer(current=f):
        return resolve_grounded_answer(current['records'], current['context'], current['answer_bindings'],
            current['document'], current['bindings'], root=eroot, now=current['now'])
    def packet(current=f, pin=None):
        return verify_packet(current['document'], current['bindings'], current['packet'],
            evidence_root=eroot, packet_root=proot, now=current['now'],
            expected_packet_sha256=pin or digest(current['packet']))
    checks = []
    def check(name, condition):
        checks.append({'name': name, 'status': 'PASS' if condition else 'FAIL'})
    e, a, p = evidence(), answer(), packet()
    check('actual_file_and_exact_value', e['status']=='VERIFIED')
    check('scoped_answer_record', a['status']=='RESOLVED' and a['record_content_verified'])
    check('complete_packet_content', p['status']=='VERIFIED' and p['packet_content_verified'])
    check('no_truth_or_execution_upgrade', not any(r['execution_authorized'] or r['truth_independently_verified'] for r in (e,a,p)))
    bad=deepcopy(f); bad['context']['kind']='unaided'
    check('unaided_requires_user', answer(bad)['reason']=='unaided_work_boundary')
    bad=deepcopy(f);bad['context']['employer_id']='other'
    check('answer_scope_preserved', answer(bad)['status']=='NEEDS_USER')
    bad=deepcopy(f);bad['context']['claim_predicate']='unrelated'
    check('answer_predicate_bound', answer(bad)['status']=='NEEDS_USER')
    bad=deepcopy(f);bad['bindings']['claims'][0]['selections'][0]['selector_id']='record'
    check('wrong_source_field_blocked', evidence(bad)['status']=='BLOCKED')
    raw=(eroot/'profile.json').read_bytes()
    (eroot/'profile.json').write_bytes(raw+b' ')
    check('changed_source_bytes_blocked', evidence()['status']=='BLOCKED')
    check('changed_source_blocks_packet', packet()['status']=='BLOCKED')
    (eroot/'profile.json').write_bytes(raw)
    bad=deepcopy(f);bad['document']['source_revision']='changed'
    try: evidence(bad); blocked=False
    except GroundingError: blocked=True
    check('changed_trust_snapshot_blocked', blocked)
    raw=(proot/'answer.txt').read_bytes()
    altered=raw+b'Unsupported extra qualification.\n'
    (proot/'answer.txt').write_bytes(altered)
    bad=deepcopy(f);bad['packet']['artifacts'][0]['sha256']=hashlib.sha256(altered).hexdigest()
    check('extra_wording_blocked_even_with_matching_file_hash', packet(bad)['status']=='BLOCKED')
    check('packet_manifest_change_blocked', packet(bad,pin=digest(f['packet']))['declared_manifest_binding_verified'] is False)
    (proot/'answer.txt').write_bytes(raw)
    bad=deepcopy(f);bad['packet']['subject_id']='other'
    check('packet_subject_bound', packet(bad)['status']=='BLOCKED')
    (proot/'attachment.bin').write_bytes(b'synthetic attachment')
    bad=deepcopy(f);bad['packet']['attachments']=[{'attachment_id':'synthetic-attachment','path':'attachment.bin',
        'sha256':hashlib.sha256(b'synthetic attachment').hexdigest()}]
    attachment=packet(bad)
    check('attachment_identity_only', attachment['status']=='VERIFIED' and
          attachment['attachments'][0]['identity_verified'] and not attachment['attachments'][0]['semantic_content_verified'])
    (proot/'attachment.bin').write_bytes(b'altered attachment')
    check('changed_attachment_blocked', packet(bad)['status']=='BLOCKED')
    dataset=json.loads((ROOT/'fixtures/grounding_eval/dataset.json').read_bytes())
    replay=json.loads((ROOT/'fixtures/grounding_eval/replay.json').read_bytes())
    evaluation=evaluate_replay(dataset,replay)
    check('evaluation_replay_labelled', evaluation['mode']=='REPLAY' and evaluation['synthetic'] is True)
    check('replay_not_model_validation', evaluation['model_inference']=='NOT_RUN' and not evaluation['model_quality_validated'])
    check('evaluation_all_cases_accounted', sum(sum(v.values()) for v in evaluation['confusion_matrix'].values())==evaluation['case_count'])
    wrong=deepcopy(replay)
    for result in wrong['results']:
        if result['case_id']=='contradicted_number':
            result['assessment'].update(verdict='PASS',findings=[])
        elif result['case_id']=='supported_exact':
            result['assessment'].update(verdict='FAIL',findings=['Synthetic deliberately wrong verdict.'])
        elif result['case_id']=='missing_source':
            result['assessment']['covered_claim_ids']=[]
    scoring_probe=evaluate_replay(dataset,wrong)
    check('false_pass_accounted',scoring_probe['metrics']['false_pass_on_nonpass_labels']['numerator']==1)
    check('false_block_accounted',scoring_probe['metrics']['false_block_on_pass_labels']['numerator']==1)
    check('malformed_response_counted_as_error',scoring_probe['metrics']['errors']['numerator']==1)
    check('no_network_or_process_attempt', not events)
    after=inventory()
    check('source_unchanged', before==after)
    report={'schema':'keel.grounding.acceptance.v1', 'status':'PASS' if all(c['status']=='PASS' for c in checks) else 'FAIL',
        'synthetic':True, 'execution_authorized':False, 'production_deployed':False,
        'release_source_sha256':after['sha256'], 'source_unchanged':before==after, 'checks':checks,
        'checks_passed':sum(c['status']=='PASS' for c in checks), 'checks_total':len(checks),
        'real_local_model_inference':'NOT_RUN', 'live_repository_integration':'NOT_VERIFIED',
        'real_data_validation':'NOT_RUN','rendered_browser':'NOT_RUN',
        'network_or_process_attempts':events, 'model_calls':0,'browser_actions':0,'canonical_writes':0,
        'evaluation_replay':evaluation,'deliberately_wrong_replay_metrics':scoring_probe['metrics']}
    (out/'acceptance.json').write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
    print(json.dumps({k:report[k] for k in ('status','checks_passed','checks_total','release_source_sha256')}))
    return 0 if report['status']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
