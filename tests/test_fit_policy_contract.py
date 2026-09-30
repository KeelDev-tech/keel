"""The intake floor is the single default; tray overrides grant no eligibility."""
import os
import sys
from pathlib import Path
from unittest.mock import patch
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import queue_intake
import fit_policy
import input_tray_digest
import ready_gate

@pytest.mark.parametrize('floor', [75, 80, 90.5])
def test_default_tracks_intake(floor):
    with patch.object(queue_intake, 'FIT_BAR', floor), patch.dict(os.environ, {}, clear=True):
        assert fit_policy.main_floor() == floor
        assert input_tray_digest._min_fit() == floor
        row = {'role_id': 'fixture', 'fit_score': floor - 1, 'action_band': 'APPLY'}
        assert 'below_fit_floor' in ready_gate.entry_admission(row, require_ready=False)['reason_codes']

@pytest.mark.parametrize('value', [True, float('nan'), float('inf'), -1, 101, '75'])
def test_invalid_canonical_floor_fails_closed(value):
    with patch.object(queue_intake, 'FIT_BAR', value), pytest.raises(ValueError):
        fit_policy.main_floor()

def test_explicit_tray_override_does_not_change_ready_policy():
    with patch.dict(os.environ, {'KEEL_TRAY_MIN_FIT': '60'}):
        assert input_tray_digest._min_fit() == 60
        assert input_tray_digest._min_fit(65) == 65
        row = {'role_id': 'fixture', 'fit_score': 65, 'action_band': 'APPLY'}
        assert 'below_fit_floor' in ready_gate.entry_admission(row, require_ready=False)['reason_codes']
