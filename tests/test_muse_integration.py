from keel_muse.integration import demo


def test_actual_files_context_admission_native_readback_and_correction(tmp_path):
    result=demo(tmp_path/'rehearsal')
    assert result['status']=='PASS'
    assert len(result['checks'])==7 and all(result['checks'].values())
    assert result['native_result']['actions_reported_applied']==11
    assert result['native_result']['native_browser_independently_verified'] is False
    assert result['actual_human_decisions']==result['real_model_calls']==result['real_browser_actions']==0
    assert result['execution_authorized'] is False
