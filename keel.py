#!/usr/bin/env python3
"""Keel's local operator interface. Python 3.11+; POSIX; no runtime dependencies."""
import argparse
import json
import os
from pathlib import Path
import sys

if sys.version_info < (3, 11):
    raise SystemExit('Keel requires Python 3.11 or newer.')
if os.name != 'posix':
    raise SystemExit('This release requires Linux, macOS, or a Linux environment such as WSL; native Windows is not validated.')

CODE = Path(__file__).resolve().parent
# Keep the historic ``keel.<subsystem>`` imports usable when this repository
# is extracted under any directory name. The same file remains the CLI; on
# import its sibling directories form the canonical package search path.
if __name__ == 'keel':
    __path__ = [str(CODE)]
    __package__ = 'keel'
    if __spec__ is not None:
        __spec__.submodule_search_locations = __path__
sys.path.insert(0, str(CODE / 'engines'))
from safe_io import atomic_json, atomic_bytes, file_lock, read_json, rows
from packet_contract import answer_receipt, confirmed_answers


def initialize(workspace):
    workspace = Path(workspace).absolute()
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    with file_lock(str(workspace / 'data/.initialize.lock')):
        templates = {
            'data/answer_bank.json': CODE / 'engines/answer_bank.example.json',
            'data/policy.json': CODE / 'engines/policy.example.json',
            'data/applicant_profile.json': CODE / 'sample_data/applicant_profile.example.json',
            'data/employer_form_patterns.json': CODE / 'engines/employer_form_patterns.example.json',
            'data/edge_case_registry.json': CODE / 'engines/edge_case_registry.example.json',
        }
        for name, source in templates.items():
            destination = workspace / name
            if not destination.exists():
                template = read_json(source)
                if name == 'data/answer_bank.json':
                    # Examples describe fields; they cannot assert identity or consent.
                    template['answers'] = {key: None for key in template['answers']}
                    template['_provenance'] = {}
                    template.setdefault('attestation_scope', {})['preauthorized_attestation_keys'] = []
                elif name == 'data/policy.json':
                    template['office']['max_office_days_per_week'] = None
                    template['office']['undefined_frequency_ok'] = None
                    template['travel']['max_travel_pct'] = None
                    template['relocation']['willing_to_relocate'] = None
                    template['relocation']['regions'] = []
                elif name == 'data/applicant_profile.json':
                    template.update(name='', headline_template='', experience=[],
                                    verified_capabilities=[], credentials=[], education='')
                    template['contact'] = {key: '' for key in template['contact']}
                atomic_json(destination, template)
            else:
                read_json(destination)  # report corruption; never replace user data with a template
        for name in ('standard', 'strategic', 'needs_input', 'rejected'):
            destination = workspace / 'data/queues' / (name + '-queue.json')
            if not destination.exists():
                atomic_json(destination, [])
            else:
                rows(read_json(destination))
        sources = workspace / 'data/sources.json'
        if not sources.exists():
            atomic_json(sources, {'schema_version': 1, 'sources': []})
        else:
            read_json(sources)
        ledger = workspace / 'data/application-ledger.json'
        if not ledger.exists():
            atomic_json(ledger, [])
        else:
            rows(read_json(ledger))
        for directory in ('data/resumes', 'data/telemetry', 'data/launch-packets', 'dashboard'):
            (workspace / directory).mkdir(mode=0o700, parents=True, exist_ok=True)
        if not (workspace / 'data/employer-blocklist.md').exists():
            atomic_bytes(workspace / 'data/employer-blocklist.md', b'# Employer blocklist\n# One exact employer name per line.\n')
    return {'initialized': True, 'workspace': str(workspace), 'next': 'Confirm your own identity, register an employer board with source-add, then discover and verify. For an isolated offline walkthrough use demo.'}


def confirm_answer(workspace, key, value, source, scope=None, expires=None):
    if not key or len(key) > 120 or not all(c.isalnum() or c == '_' for c in key):
        raise ValueError('answer key must contain letters, digits or underscores')
    if value is None or value == '':
        raise ValueError('an explicit nonempty answer is required')
    path = Path(workspace) / 'data/answer_bank.json'
    with file_lock(str(path) + '.lock'):
        bank = read_json(path)
        if not isinstance(bank, dict):
            raise ValueError('initialize the workspace first')
        bank.setdefault('answers', {})[key] = value
        bank.setdefault('_provenance', {})[key] = answer_receipt(value, source, role_id=scope, expires_at=expires)
        atomic_json(path, bank)
    return {'recorded_applicant_assertion': key, 'scope': scope or 'general'}


def doctor(workspace, capabilities=False):
    workspace = Path(workspace)
    checks = []
    def add(name, ok, detail):
        checks.append({'check': name, 'status': 'PASS' if ok else 'ACTION_REQUIRED', 'detail': detail})
    bank = None
    try:
        bank = read_json(workspace / 'data/answer_bank.json')
        answers, problems = confirmed_answers(bank, role_id='general')
        missing = [k for k in ('first_name', 'last_name', 'email') if k not in answers]
        add('Applicant assertions', not missing and not problems, '; '.join(problems + missing) or 'Required identity assertions are recorded; their truth is asserted by the applicant.')
    except (OSError, ValueError, TypeError) as exc:
        add('Applicant assertions', False, str(exc))
    try:
        policy = read_json(workspace / 'data/policy.json')
        if not isinstance(policy, dict) or not policy:
            raise ValueError('policy missing or invalid; initialize or restore it')
        add('Workspace policy', True, 'Readable; unset personal preferences remain unknown.')
    except (OSError, ValueError, TypeError) as exc:
        add('Workspace policy', False, str(exc))
    all_entries = []
    for name in ('standard', 'strategic', 'needs_input'):
        path = workspace / 'data/queues' / (name + '-queue.json')
        try:
            if not path.is_file():
                raise ValueError('queue missing; run init')
            entries = rows(read_json(path))
            all_entries.extend((name, entry) for entry in entries)
            add(name + ' queue', True, str(len(entries)) + ' entries')
        except (OSError, ValueError) as exc:
            add(name + ' queue', False, str(exc))
    seen, duplicates, invalid = set(), set(), 0
    for name, entry in all_entries:
        rid = entry.get('role_id')
        if not isinstance(rid, str) or not rid:
            invalid += 1
        elif rid in seen:
            duplicates.add(rid)
        else:
            seen.add(rid)
    from packet_contract import source_url
    from safe_io import contained_path, file_digest
    preparation_gaps = []
    for origin, entry in all_entries:
        if entry.get('status') not in ('READY', 'READY-FOR-BROWSER'):
            continue
        try:
            source_url(entry)
            material = entry.get('materials') or {}
            resume = material.get('resume') or ('data/resumes/' + entry['resume_version'] if origin == 'strategic' and entry.get('resume_version') else None)
            if not resume:
                raise ValueError('resume missing')
            file_digest(contained_path(workspace, resume))
            if material.get('cover_letter'):
                file_digest(contained_path(workspace, material['cover_letter']))
            if bank is not None:
                _, problems = confirmed_answers(bank, role_id=entry.get('role_id'))
                if problems:
                    raise ValueError('; '.join(problems))
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            preparation_gaps.append(str(entry.get('role_id', 'Unknown role')) + ': ' + str(exc))
    add('READY preparation inputs', not preparation_gaps, '; '.join(preparation_gaps[:20]) or 'No missing URL/material inputs found; live form review still required.')
    add('Queue identity', not duplicates and not invalid,
        f'{len(duplicates)} duplicate role identifiers; {invalid} missing identifiers')
    try:
        ledger = workspace / 'data/application-ledger.json'
        if not ledger.is_file():
            raise ValueError('ledger missing; run init')
        rows(read_json(ledger))
        add('Ledger structure', True, 'Readable. Status labels alone do not prove outcomes.')
    except (OSError, ValueError) as exc:
        add('Ledger structure', False, str(exc))
    from build_dashboard import collect
    # Optional observation sources do not gate local packet preparation.
    # Required queues, ledger and preparation inputs were checked above.
    observation_warnings = collect(workspace)['warnings']
    add('Public boundary', True, 'Preparation only; no paid APIs or application submission are implemented by this CLI.')
    result = {'ready_for_local_preparation': all(c['status'] == 'PASS' for c in checks),
            'provider_verification_available': False, 'checks': checks,
            'observation_warnings': observation_warnings}
    if capabilities:
        from tools.release_profile import capability_report
        result['capabilities'] = capability_report(workspace)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', default=os.environ.get('KEEL_HOME', str(Path.home() / 'keel')))
    parser.add_argument('--role', default='operator', choices=['operator', 'discovery', 'preparation', 'evidence', 'qa', 'security', 'ux', 'reliability'])
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init', help='create missing workspace files; preserve existing data')
    doctor_parser = sub.add_parser('doctor', help='offline workspace checks')
    doctor_parser.add_argument('--capabilities', action='store_true',
                               help='include offline runtime and integration inventory')
    sub.add_parser('dashboard', help='build an offline snapshot')
    prepare = sub.add_parser('prepare', help='prepare review packets with bounded public HTTPS reads')
    prepare.add_argument('--all', action='store_true',
                         help='legacy alias; the bounded buffer capacity still applies')
    prepare.add_argument('--refresh', action='store_true', help='refill the screened packet buffer')
    confirm = sub.add_parser('confirm-answer', help='record an answer explicitly asserted by the applicant')
    confirm.add_argument('--key', required=True)
    confirm.add_argument('--value', help='omit to read a value from stdin without command-history exposure')
    confirm.add_argument('--source', required=True)
    confirm.add_argument('--scope')
    confirm.add_argument('--expires')
    validate = sub.add_parser('validate-packet', help='check a packet against current workspace inputs')
    validate.add_argument('packet')
    sub.add_parser('roles', help='show review roles and their supported CLI actions')
    sub.add_parser('supply', help='disjoint supply states and exact question groups; no inferred answers')
    sub.add_parser('pipeline-doctor', help='offline queue conservation and READY admission losses; no operational writes')
    muse_census = sub.add_parser('muse-census', help='bound local census and Muse adapter contract; imported JSON cannot grant authority')
    muse_census.add_argument('--host-snapshot', help='optional unauthenticated observation file inside the workspace')
    supply_plan = sub.add_parser('supply-plan', help='read-only fair cohort plan at the canonical fit floor')
    supply_plan.add_argument('--limit', type=int, default=25)
    supply_plan.add_argument('--max-requests', type=int, default=10)
    supply_canary = sub.add_parser('supply-canary', help='bounded public posting check; never READY promotion or submission')
    supply_canary.add_argument('--plan-id', required=True)
    supply_canary.add_argument('--run-id')
    supply_canary.add_argument('--limit', type=int, default=25)
    supply_canary.add_argument('--max-requests', type=int, default=10)
    supply_canary.add_argument('--timeout', type=int, default=30)
    supply_canary.add_argument('--live', action='store_true', help='persist journal and posting observations only')
    measurements = sub.add_parser('supply-measurements', help='read-only pseudonymous stages and retained request evidence')
    measurements.add_argument('--key-file', required=True, help='32-byte correlation key inside workspace')
    measurements.add_argument('--before', help='previous metadata snapshot inside workspace')
    measurements.add_argument('--failure-cases', action='store_true', help='export bounded synthetic replay recipes')
    sub.add_parser('supply-reconcile', help='inspect an interrupted canary; no automatic closure or replay')
    host_preflight = sub.add_parser('host-preflight',
        help='offline, non-migrating checks before a bounded public trial')
    productivity_status = sub.add_parser('productivity-status',
        help='inspect live pipeline bottlenecks and recorded efficiency; no network reads')
    productivity_once = sub.add_parser('productivity-once',
        help='plan or run one budgeted public discovery/verification stage')
    productivity_recover = sub.add_parser('productivity-recover',
        help='reconcile one recorded local run; preserve unknown usage and application holds')
    productivity_recover.add_argument('--run-id', required=True)
    productivity_trial = sub.add_parser('productivity-trial',
        help='plan or run a fixed, restart-safe cohort of public pipeline cycles')
    productivity_trial.add_argument('--trial-id', required=True)
    productivity_trial.add_argument('--label', default='candidate', help='operator label, not evidence of quality')
    productivity_trial.add_argument('--max-cycles', type=int, default=3,
                                    help='fixed cohort size, 1..100; stops early on idle or held work')
    productivity_report = sub.add_parser('productivity-trial-report',
        help='inspect a trial using its actual retained work receipts')
    productivity_report.add_argument('--trial-id', required=True)
    productivity_compare = sub.add_parser('productivity-compare',
        help='compare actual trial cohorts; mismatched workloads cannot establish gains')
    productivity_compare.add_argument('--baseline', required=True)
    productivity_compare.add_argument('--candidate', required=True)
    productivity_advice = sub.add_parser('productivity-advice',
        help='diagnose canonical trial evidence and propose a budget-bounded next cohort; no dispatch')
    productivity_advice.add_argument('--trial-id', required=True)
    productivity_advice.add_argument('--max-cycles', type=int, default=3,
                                      help='upper bound on proposed follow-up cycles, 1..100')
    question_resolver = sub.add_parser('qresolve',
        help='find exact scoped answers in local evidence; dry-run by default')
    question_resolver.add_argument('--live', action='store_true',
        help='save drafts; automatic FACT reuse additionally requires explicit local authorization')
    question_resolver.add_argument('--max-cards', type=int, default=50)
    question_supply = sub.add_parser('qresolve-supply',
        help='diagnose resolved blockers and observed READY outcomes; no network or promotion')
    question_supply.add_argument('--live', action='store_true',
        help='save bounded READY observations in private resolution metadata only')
    question_recover = sub.add_parser('qresolve-recover',
        help='inspect interrupted answer applications; close only proven outcomes')
    question_recover.add_argument('--decision-id')
    question_recover.add_argument('--live', action='store_true',
        help='record a proven recovery for one selected decision; never replay queue writes')
    question_approve = sub.add_parser('qresolve-approve',
        help='approve an exact reviewed FACT or JUDGMENT draft without changing its source')
    question_approve.add_argument('--decision-id', required=True)
    question_approve.add_argument('--live', action='store_true',
        help='apply this exact draft to its current target roles after evidence revalidation')
    for command in (host_preflight, productivity_status, productivity_once, productivity_recover,
                    productivity_trial, productivity_report, productivity_compare, productivity_advice):
        command.add_argument('--budget-ledger', default=os.environ.get('KEEL_BUDGET_LEDGER'),
                             help='existing resource ledger shared with other workers')
        command.add_argument('--budget-scope', default=os.environ.get('KEEL_BUDGET_SCOPE'),
                             help='existing cumulative budget scope; never resets automatically')
    for command in (host_preflight, productivity_status, productivity_once, productivity_trial):
        command.add_argument('--target-verified', type=int, default=20,
                             help='pause intake at this many fresh posting-presence observations')
        command.add_argument('--backlog-limit', type=int, default=200,
                             help='pause intake when this many queue rows already exist')
    host_preflight.add_argument('--max-requests', type=int, default=8,
                                help='requested per-cycle reader dispatch cap to inspect')
    host_preflight.add_argument('--timeout', type=float, default=30,
                                help='requested cooperative cycle deadline in seconds')
    for command in (productivity_once, productivity_trial):
        command.add_argument('--live', action='store_true',
                             help='allow bounded public reads and local receipt/queue writes')
        command.add_argument('--max-requests', type=int, default=8,
                             help='per-cycle reader dispatch cap, excluding redirects and socket retries')
        command.add_argument('--timeout', type=float, default=30,
                             help='per-cycle cooperative deadline in seconds')
        command.add_argument('--max-boards', type=int, default=2)
        command.add_argument('--max-new', type=int, default=50)
        command.add_argument('--verify-limit', type=int, default=100)
        command.add_argument('--title', action='append', default=[])
        command.add_argument('--location', action='append', default=[])
    productivity_once.add_argument('--run-id', help='stable idempotency key; required with --live')
    receipt_review = sub.add_parser('receipt-review',
        help='inspect exact receipt/ledger/queue binding; no provider adapter or outcome writes')
    receipt_review.add_argument('--role-id', required=True)
    receipt_review.add_argument('--attempt-id', required=True)
    receipt_review.add_argument('--receipts', required=True, help='existing receipt store inside the workspace')
    source = sub.add_parser('source-add', help='register an exact employer board; no keys required')
    source.add_argument('ref', help='greenhouse:board, lever:board, lever_eu:board or ashby:board')
    source.add_argument('--company', help='your display label for this employer')
    discovery = sub.add_parser('discover', help='read registered public boards and deduplicate new leads')
    discovery.add_argument('--max-new', type=int, default=200)
    discovery.add_argument('--timeout', type=float, default=120)
    discovery.add_argument('--title', action='append', default=[])
    discovery.add_argument('--location', action='append', default=[])
    verification = sub.add_parser('verify', help='verify exact posting presence in board cohorts, never approve submissions')
    verification.add_argument('--live', action='store_true', help='commit observations; otherwise inspect only')
    verification.add_argument('--limit', type=int, default=100)
    verification.add_argument('--timeout', type=float, default=120)
    one = sub.add_parser('prepare-role', help='explicitly select a verified role for a human-review packet')
    one.add_argument('role_id')
    one.add_argument('--resume', required=True, help='path inside the workspace, e.g. data/resumes/resume.pdf')
    sub.add_parser('flush-outbox', help='replay committed observation events idempotently')
    sub.add_parser('demo', help='offline synthetic walkthrough; requires a new empty workspace')
    args = parser.parse_args(argv)
    os.environ['KEEL_HOME'] = str(Path(args.home).absolute())
    roles = read_json(CODE / 'roles.json')
    if args.command not in roles['roles'][args.role]['cli_actions']:
        parser.error('role does not allow this CLI action')
    try:
        if args.command == 'demo':
            from first_run_demo import run_demo
            result = run_demo(args.home)
        elif args.command == 'qresolve-approve':
            import tray_answer
            try:
                return tray_answer.main(['--approve-qresolve', args.decision_id] +
                                        (['--live'] if args.live else []))
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                raise ValueError('qresolve_approval_input_or_state_invalid') from None
        elif args.command == 'qresolve-recover':
            import qresolve_recovery
            try:
                result = qresolve_recovery.recover(
                    args.home, decision_id=args.decision_id, live=args.live)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                raise ValueError('qresolve_recovery_input_or_state_invalid') from None
        elif args.command == 'qresolve-supply':
            import qresolve_outcomes
            try:
                result = qresolve_outcomes.console_report(qresolve_outcomes.observe(
                    args.home, live=args.live))
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                raise ValueError('qresolve_supply_input_or_state_invalid') from None
        elif args.command == 'qresolve':
            import qresolve
            try:
                result = qresolve.console_report(qresolve.run(
                    args.home, live=args.live, max_cards=args.max_cards))
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                raise ValueError('qresolve_input_or_state_invalid') from None
        elif args.command == 'host-preflight':
            import host_readiness
            result = host_readiness.inspect(args.home, budget_ledger=args.budget_ledger,
                budget_scope=args.budget_scope, target_verified=args.target_verified,
                backlog_limit=args.backlog_limit, max_requests=args.max_requests, timeout=args.timeout)
        elif args.command == 'productivity-advice':
            from keel_efficiency.defaults import configured_budget
            import productivity_advisor
            ledger, scope = configured_budget(args.budget_ledger, args.budget_scope, readonly=True)
            result = productivity_advisor.diagnose(args.home, trial_id=args.trial_id,
                ledger=ledger, scope_id=scope, max_cycles=args.max_cycles)
        elif args.command == 'receipt-review':
            import receipt_projection
            result = receipt_projection.reconcile(args.home, role_id=args.role_id,
                attempt_id=args.attempt_id, receipts_path=args.receipts, live=False)
        elif args.command in {'productivity-status', 'productivity-once', 'productivity-recover',
                              'productivity-trial', 'productivity-trial-report', 'productivity-compare'}:
            from keel_efficiency.defaults import configured_budget
            import productivity_service
            if args.budget_ledger is not None and not Path(args.budget_ledger).is_file():
                raise ValueError('productivity requires an existing operator-created budget ledger')
            read_only = args.command in {'productivity-status', 'productivity-trial-report',
                                         'productivity-compare'} or (
                args.command in {'productivity-once', 'productivity-trial'} and not args.live)
            ledger, scope = configured_budget(args.budget_ledger, args.budget_scope,
                                              readonly=read_only)
            options = {'ledger': ledger, 'scope_id': scope}
            if args.command in {'productivity-status', 'productivity-once', 'productivity-trial'}:
                options.update(target_verified=args.target_verified, backlog_limit=args.backlog_limit)
            if args.command == 'productivity-recover':
                result = productivity_service.recover(args.home, run_id=args.run_id, **options)
            elif args.command == 'productivity-status':
                result = productivity_service.status(args.home, **options)
            elif args.command in {'productivity-trial-report', 'productivity-compare'}:
                import productivity_trials
                if args.command == 'productivity-trial-report':
                    result = productivity_trials.trial_report(args.home, trial_id=args.trial_id, **options)
                else:
                    result = productivity_trials.compare_trials(args.home,
                        baseline_trial_id=args.baseline, candidate_trial_id=args.candidate, **options)
            else:
                options.update(live=args.live, max_requests=args.max_requests, timeout=args.timeout,
                    max_boards=args.max_boards, max_new=args.max_new, verify_limit=args.verify_limit,
                    titles=tuple(args.title), locations=tuple(args.location))
                if args.command == 'productivity-trial':
                    import productivity_trials
                    result = productivity_trials.run_trial(args.home, trial_id=args.trial_id,
                        label=args.label, max_cycles=args.max_cycles, **options)
                else:
                    result = productivity_service.run_once(args.home, run_id=args.run_id, **options)
        elif args.command in {'source-add', 'discover', 'verify', 'supply', 'prepare-role', 'flush-outbox'}:
            import pipeline_service as service
            if args.command == 'source-add':
                result = service.add_source(args.home, args.ref, args.company)
            elif args.command == 'discover':
                result = service.discover(args.home, max_new=args.max_new, timeout=args.timeout, titles=args.title, locations=args.location)
            elif args.command == 'verify':
                result = service.verify(args.home, limit=args.limit, timeout=args.timeout, live=args.live)
            elif args.command == 'prepare-role':
                result = service.prepare_role(args.home, args.role_id, args.resume)
            elif args.command == 'flush-outbox':
                result = service.flush_outbox(args.home)
            else:
                result = service.supply_report(args.home)
        elif args.command == 'pipeline-doctor':
            import pipeline_doctor
            result = pipeline_doctor.report(args.home)
        elif args.command == 'supply-measurements':
            import supply_measurements
            if args.before and args.failure_cases:
                raise ValueError('choose comparison or failure cases')
            result = supply_measurements.report(args.home, key_file=args.key_file, before_file=args.before, cases=args.failure_cases)
        elif args.command == 'muse-census':
            import muse_bridge
            result = muse_bridge.census(args.home, host_snapshot=args.host_snapshot)
        elif args.command in {'supply-plan', 'supply-canary', 'supply-reconcile'}:
            import supply_recovery
            if args.command == 'supply-plan':
                result = supply_recovery.plan(args.home, limit=args.limit, max_requests=args.max_requests)
            elif args.command == 'supply-reconcile':
                result = supply_recovery.recovery_status(args.home)
            else:
                result = supply_recovery.canary(args.home, plan_id=args.plan_id, run_id=args.run_id,
                    limit=args.limit, max_requests=args.max_requests, timeout=args.timeout, live=args.live)
        elif args.command == 'init':
            result = initialize(args.home)
        elif args.command == 'confirm-answer':
            value = args.value if args.value is not None else input('Applicant-asserted value: ')
            result = confirm_answer(args.home, args.key, value, args.source, args.scope, args.expires)
        elif args.command == 'doctor':
            result = doctor(args.home, capabilities=args.capabilities)
        elif args.command == 'dashboard':
            from build_dashboard import build
            result = {'dashboard': str(build(args.home))}
        elif args.command == 'prepare':
            # Public preparation stops at a screened buffer. It cannot create
            # unowned IN-FLIGHT rows by calling the historical claim loop.
            import apply_loop
            count = apply_loop.refresh_buffer()
            result = {'fresh_buffer_packets': count, 'execution_authorized': False,
                      'queue_claimed': False, 'scope': 'screened_packet_buffer'}
        elif args.command == 'validate-packet':
            import apply_loop
            import packet_contract
            from safe_io import contained_path
            packet = read_json(contained_path(Path(args.home) / 'data/launch-packets', args.packet))
            entry, origin = apply_loop._current_entry(packet['role_id'])
            packet_contract.validate(packet, entry, apply_loop.load_answer_bank(), apply_loop.load_policy(),
                                     args.home, apply_loop._materials_for(entry, origin))
            result = {'valid_preparation_packet': True, 'execution_authorized': False}
        else:
            result = roles
        print(json.dumps(result, indent=2))
        if args.command == 'host-preflight':
            return 0 if result['local_checks_passed'] else 1
        if args.command == 'productivity-advice':
            return 1 if result['status'] == 'HOLD' else 0
        if args.command in {'qresolve', 'qresolve-recover', 'qresolve-supply'}:
            return 1 if result.get('status') == 'HOLD' else 0
        return 1 if args.command == 'doctor' and not result['ready_for_local_preparation'] else 0
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        print(json.dumps({'error': str(exc), 'action': args.command}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
