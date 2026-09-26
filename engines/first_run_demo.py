"""Offline, isolated first-run walkthrough. Every record is synthetic.

No network connection, paid service, applicant identity, external account or
application submission is used. Existing workspaces are never repurposed.
"""
from pathlib import Path
from unittest.mock import patch
import os
from safe_io import atomic_json, atomic_bytes, read_json, rows


def run_demo(workspace):
    root = Path(workspace).absolute()
    if root.exists():
        raise ValueError('demo requires a new, nonexistent workspace directory')
    root.mkdir(parents=True, mode=0o700)
    atomic_json(root/'DEMO_ONLY.json', {'synthetic': True, 'external_actions_allowed': False})
    import keel
    from pipeline_service import add_source, discover, verify, PublicBoardReader, prepare_role, supply_report
    import apply_loop
    import packet_contract
    keel.initialize(root)
    for key, value in [('first_name', 'River'), ('last_name', 'Sampleton'), ('email', 'river@example.invalid')]:
        keel.confirm_answer(root, key, value, 'SYNTHETIC OFFLINE FIXTURE; not a real applicant assertion')
    atomic_bytes(root/'data/resumes/fixture.md', b'# Synthetic offline fixture\nNo real experience or qualifications asserted.\n')
    add_source(root, 'greenhouse:synthetic-demo', 'Synthetic Demo Employer')
    calls = []
    def fixture_fetch(url, timeout):
        calls.append(url)
        assert url.startswith('https://boards-api.greenhouse.io/v1/boards/synthetic-demo/jobs?')
        return {'jobs': [{'id': 100+i, 'title': title, 'location': {'name': 'Synthetic location'},
                          'content': 'Synthetic posting. Not a real vacancy.'}
                         for i, title in enumerate(['Operations Review Fixture', 'Customer Support Review Fixture', 'Technology Review Fixture'], 1)]}
    first = discover(root, reader=PublicBoardReader(fetcher=fixture_fetch))
    second = discover(root, reader=PublicBoardReader(fetcher=fixture_fetch))
    result = verify(root, live=True, reader=PublicBoardReader(fetcher=fixture_fetch))
    entry = rows(read_json(root/'data/queues/standard-queue.json'))[0]
    intel = {'ats': 'synthetic_fixture', 'form_url': entry['application_url'], 'questions': [],
             'rendered_option_fetch_needed': [], 'extraction_complete': False,
             'source': 'OFFLINE_SYNTHETIC_FIXTURE'}
    # Only transport is replaced. Queue, ledger, lease, packet/hash, prescreen and
    # storage implementations are the actual shipped code.
    with patch.object(apply_loop, 'live', return_value=None), patch.object(apply_loop.form_intel, 'probe_url', return_value=intel):
        packet_result = prepare_role(root, entry['role_id'], 'data/resumes/fixture.md', _offline_fixture=True)
    packet = read_json(packet_result['packet'])
    report = {'synthetic': True, 'external_network_requests': 0, 'fixture_board_reads': len(calls),
              'discovery': first, 'second_discovery': second, 'verification': result,
              'packet': packet_result, 'supply': supply_report(root),
              'checks': {'three_roles_ingested': first['added'] == 3,
                         'replay_deduplicated': second['added'] == 0,
                         'one_board_read_for_three_verifications': result['requests'] == 1,
                         'no_ready_promotion': result['promoted_to_ready'] == 0,
                         'packet_never_authorizes_execution': packet['execution_authorized'] is False,
                         'synthetic_email_only': packet['applicant_assertions']['email'].endswith('.invalid')}}
    atomic_json(root/'demo-report.json', report)
    from build_dashboard import build
    try:
        report['dashboard'] = str(build(root))
    except (TypeError, ValueError, KeyError, OSError) as exc:
        report['dashboard_note'] = 'Optional legacy dashboard unavailable: '+type(exc).__name__
    report['report_path'] = str(root/'demo-report.json')
    atomic_json(root/'demo-report.json', report)
    return report
