"""Evidence lineage and integrated report regressions, all synthetic."""
from copy import deepcopy
from datetime import timedelta
import json
import pytest
from tools.make_trust_demo import NOW, evidence, make_document
from keel_trust.common import ContractError, digest
from keel_trust.evidence import evaluate
from keel_trust.report import build, markdown
from keel_trust.__main__ import main


def test_evidence_reports_basis_without_asserting_truth_or_authority():
    doc = evidence(); old = deepcopy(doc); report = evaluate(doc, now=NOW)
    assert doc == old and report['current'] and not report['invalidated_artifact_ids']
    assert report['claims'][0]['basis'] == 'SELF_ATTESTED'
    assert report['claims'][0]['truth_independently_verified'] is False
    assert not report['sources'][0]['can_issue_instructions'] and not report['execution_authorized']


@pytest.mark.parametrize('field,value', [('revision','v2'),('content_hash','a'*64),('status','DISPUTED'),('status','REVOKED'),
    ('expires_at',NOW.isoformat()),('observed_at',(NOW+timedelta(seconds=1)).isoformat())])
def test_source_invalidates_every_dependent_material(field,value):
    doc=evidence();doc['sources'][0][field]=value
    if field=='expires_at':doc['sources'][0]['observed_at']=(NOW-timedelta(minutes=1)).isoformat()
    assert set(evaluate(doc,now=NOW)['invalidated_artifact_ids']) == {'resume','packet','interview'}


@pytest.mark.parametrize('field,value',[('review_state','PENDING'),('review_state','DISPUTED'),('approval_ref',None),
    ('expires_at',NOW.isoformat()),('revision','v2'),('allowed_scopes',['different']),('allowed_wording',['different'])])
def test_unapproved_claim_or_changed_wording_cannot_reach_materials(field,value):
    doc=evidence();doc['claims'][0][field]=value
    assert len(evaluate(doc,now=NOW)['invalidated_artifact_ids']) == 3


@pytest.mark.parametrize('change',['partial','stale','future'])
def test_export_must_be_current(change):
    doc=evidence()
    if change=='partial':doc['complete']=False
    else:doc['observed_at']=(NOW+timedelta(seconds=1 if change=='future' else -91)).isoformat()
    report=evaluate(doc,now=NOW)
    assert not report['current'] and len(report['invalidated_artifact_ids'])==3


@pytest.mark.parametrize('change',['source_workspace','claim_workspace','artifact_workspace','consent_kind','duplicate_source','missing_source','self_dependency'])
def test_evidence_contract_refuses_ambiguous_or_cross_workspace_input(change):
    doc=evidence()
    if change.endswith('_workspace'):doc[{'source_workspace':'sources','claim_workspace':'claims','artifact_workspace':'artifacts'}[change]][0]['workspace_id']='acme-private'
    if change=='consent_kind':doc['claims'][0]['kind']='CONSENT'
    if change=='duplicate_source':doc['sources'].append(deepcopy(doc['sources'][0]))
    if change=='missing_source':doc['claims'][0]['evidence'][0]['source_id']='missing'
    if change=='self_dependency':doc['artifacts'][0]['depends_on']=['resume']
    with pytest.raises(ContractError):evaluate(doc,now=NOW)


def test_cycles_block_cycle_and_descendants():
    doc=evidence();doc['artifacts'][0]['depends_on']=['packet']
    report=evaluate(doc,now=NOW)
    assert len(report['invalidated_artifact_ids'])==3
    assert all('DEPENDENCY_CYCLE_OR_BLOCKED_BY_CYCLE' in r['reasons'] for r in report['artifacts'])


def test_dependency_cannot_launder_claim_into_another_scope():
    doc=evidence();doc['artifacts'][1]['scope']='other-project'
    assert set(evaluate(doc,now=NOW)['invalidated_artifact_ids'])=={'packet','interview'}


@pytest.mark.parametrize('separate_scope,revoked,blocked',[(False,False,True),(True,False,False),(False,True,False)])
def test_contradiction_is_scoped_and_revoked_history_is_not_new_evidence(separate_scope,revoked,blocked):
    doc=evidence();claim=deepcopy(doc['claims'][0]);claim.update(claim_id='conflict',value_hash='b'*64)
    if separate_scope:claim['allowed_scopes']=['other-scope']
    if revoked:claim['review_state']='REVOKED'
    doc['claims'].append(claim)
    first=next(r for r in evaluate(doc,now=NOW)['claims'] if r['claim_id']=='claim-example')
    assert ('CONTRADICTORY_CLAIMS' in first['reasons']) is blocked


def test_explicit_conflict_is_symmetric():
    doc=evidence();other=deepcopy(doc['claims'][0]);other.update(claim_id='second',predicate='other-predicate',conflicts_with=['claim-example'])
    doc['claims'].append(other)
    assert all('CONTRADICTORY_CLAIMS' in c['reasons'] for c in evaluate(doc,now=NOW)['claims'])


@pytest.mark.parametrize('basis,origin,reason', [('EMPLOYER_STATED','APPLICANT_RECORD','BASIS_UNSUPPORTED'),
    ('SELF_ATTESTED','EXTERNAL_UNTRUSTED','BASIS_UNSUPPORTED'),('ADAPTER_VERIFIED','EXTERNAL_UNTRUSTED','VERIFICATION_REFERENCE_MISSING'),
    ('CORROBORATED','APPLICANT_RECORD','CORROBORATION_INSUFFICIENT')])
def test_evidence_labels_require_support(basis,origin,reason):
    doc=evidence();doc['claims'][0]['basis']=basis;doc['sources'][0].update(origin=origin,verification_ref='asserted-reference')
    assert reason in evaluate(doc,now=NOW)['claims'][0]['reasons']


@pytest.mark.parametrize('same_publisher',[True,False])
def test_duplicate_publisher_is_not_corroboration(same_publisher):
    doc=evidence();source=deepcopy(doc['sources'][0]);source.update(source_id='second',origin='INDEPENDENT')
    if not same_publisher:source['publisher_id']='independent-publisher'
    doc['sources'].append(source);doc['claims'][0]['basis']='CORROBORATED'
    doc['claims'][0]['evidence'].append({k:source[k] for k in ('source_id','revision','content_hash')})
    assert ('CORROBORATION_INSUFFICIENT' in evaluate(doc,now=NOW)['claims'][0]['reasons']) is same_publisher


def test_integrated_changed_evidence_blocks_readiness_and_future_supply_without_writes():
    doc=make_document();before=deepcopy(doc);good=build(doc,now=NOW)
    assert doc==before and good['flow']['readiness']['executable_ready']==1
    doc['evidence_export']['sources'][0]['revision']='v2';changed=deepcopy(doc)
    bad=build(doc,now=NOW)
    assert doc==changed and bad['original_executable_estimate']==1
    assert bad['flow']['readiness']['executable_ready']==0
    assert bad['excluded_release_ids']==['synthetic-backlog']
    assert bad['operator_brief']['selected']==[]
    assert not any(bad['effects'].values()) and not bad['execution_authorized']
    assert bad['twin']['export']['leads'][0]['packet_present'] is False


@pytest.mark.parametrize('change',['missing','revision','hash','packet_hash'])
def test_material_binding_cannot_silently_reuse_old_packet(change):
    doc=make_document()
    if change=='missing':doc['artifact_bindings'].pop(0)
    else:doc['artifact_bindings'][0][{'revision':'artifact_revision','hash':'artifact_sha256','packet_hash':'packet_dependency_hash'}[change]]='b'*64
    report=build(doc,now=NOW)
    assert report['flow']['readiness']['executable_ready']==0 and report['material_holds'][0]['role_id']=='role-1'


@pytest.mark.parametrize('change',['wrapper_stale','wrapper_partial','evidence_partial','flow_partial'])
def test_stale_integrated_view_suppresses_decisions_and_research(change):
    doc=make_document()
    if change=='wrapper_stale':doc['observed_at']=(NOW-timedelta(seconds=91)).isoformat()
    elif change=='wrapper_partial':doc['complete']=False
    elif change=='evidence_partial':doc['evidence_export']['complete']=False
    else:doc['flow_export']['complete']=False
    report=build(doc,now=NOW)
    assert report['status']=='UNVERIFIED'
    assert not report['operator_brief']['selected'] and not report['research']['selected']


def test_report_cli_is_useful_and_cannot_overwrite(tmp_path,capsys):
    source=tmp_path/'input.json';source.write_text(json.dumps(make_document()));out=tmp_path/'report.md'
    args=['report',str(source),'--now',NOW.isoformat(),'--format','markdown','--out',str(out)]
    assert main(args)==0 and 'After evidence gates | 1' in out.read_text()
    old=out.read_bytes();assert main(args)==2 and out.read_bytes()==old
    assert main(['evidence',str(source),'--now',NOW.isoformat()])==2
    assert 'keel-trust:' in capsys.readouterr().err


def test_report_retains_owner_and_never_answers_consent():
    report=build(make_document(),now=NOW)
    consent=next(r for r in report['operator_brief']['selected'] if r['classification']=='CONSENT_QUARANTINED')
    assert consent['operator_reply_required'] and consent['resolved'] is False
    assert report['operator_brief']['answers_generated']==0 and 'reply required' in markdown(report)
