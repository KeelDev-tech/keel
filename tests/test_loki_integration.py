from keel_loki.integration import demo


def test_source_fact_plan_then_correction_blocks_reuse(tmp_path):
    result=demo(tmp_path/'integrated')
    assert result['stale_fact_rejected']
    assert result['initial']['bound_fact_fields']['motivation']['source_sha256']==result['source_bytes_sha256']
    assert len(result['initial']['plan']['fields'])==12
    assert not result['initial']['all_material_claims_enumerated']
    assert not result['initial']['human_approval_authenticated']
    assert result['browser_actions']==result['real_model_calls']==result['canonical_writes']==0
