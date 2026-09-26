"""Evolution v2: artifact-bound qualification, revocation, quotas and recovery."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

from keel_machine.common import canonical, clone, digest, ident, require, sha, _private
from keel_eval.evaluation import validate_dataset, dataset_digest
from keel_eval.reliability import Runner, freeze_plan, evaluate_trials
from .interpreter import artifact_runner, baseline_runner, validate_rule

ROW_LIMIT = 4096
BYTE_LIMIT = 16 * 1024 * 1024
PAGE_LIMIT = 16384
TTL = 86400
TABLES = ('evidence', 'artifacts', 'scenarios', 'evaluations', 'community_candidates',
          'evolution_lifecycle', 'evolution_datasets', 'evolution_subjects',
          'evolution_trials', 'evolution_feedback')


class Hardened:
    def __init__(self, home, *, clock=time.time):
        require((Path(home) / 'evolution.sqlite3').is_file() or getattr(self, '_creating', False),
                'evolution_missing_database_use_create')
        super().__init__(home, clock=clock)
        with self.db.transaction() as db:
            db.execute('PRAGMA max_page_count=16384')
            db.execute('CREATE TABLE IF NOT EXISTS evolution_lifecycle(artifact_id TEXT PRIMARY KEY, expires_at REAL NOT NULL, revision INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS evolution_datasets(dataset_sha256 TEXT PRIMARY KEY, split TEXT NOT NULL, document BLOB NOT NULL, ordinal INTEGER NOT NULL, used_by TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS evolution_subjects(subject_sha256 TEXT PRIMARY KEY, split TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS evolution_trials(evaluation_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, body_sha256 TEXT NOT NULL, receipt BLOB NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS evolution_feedback(feedback_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, evidence_id TEXT NOT NULL, reason TEXT NOT NULL)')
            # Old promotions were not artifact-bound. They require new qualification.
            rows = db.execute('SELECT artifact_id FROM artifacts WHERE artifact_id NOT IN (SELECT artifact_id FROM evolution_lifecycle)').fetchall()
            for row in rows:
                db.execute("UPDATE artifacts SET state='HELD',reason='legacy_unbound_qualification' WHERE artifact_id=?", (row[0],))
                db.execute('INSERT INTO evolution_lifecycle VALUES(?,?,0)', (row[0], 0))
            self._usage(db)

    @classmethod
    def create(cls, home, *, clock=time.time):
        from keel_machine.common import _ancestors
        home = Path(os.path.abspath(home))
        require(home.parent.resolve(strict=True) == home.parent, 'evolution_parent_symlink')
        _ancestors(home)
        home.mkdir(mode=0o700, exist_ok=False)
        instance = cls.__new__(cls)
        instance._creating = True
        instance.__init__(home, clock=clock)
        instance._creating = False
        instance._sync_home()
        return instance

    def _sync_home(self):
        for path in (self.home, self.home.parent):
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    @staticmethod
    def _usage(db):
        total = 0
        for table in TABLES:
            # Only the constant internal table allowlist enters SQL identifiers.
            count = db.execute('SELECT COUNT(*) FROM "' + table + '"').fetchone()[0]  # nosec B608
            require(count <= ROW_LIMIT, 'evolution_row_quota')
            columns = db.execute('PRAGMA table_info("' + table + '")').fetchall()  # nosec B608
            names = [row[1] for row in columns]
            require(all(name.replace('_','').isalnum() for name in names), 'storage_columns_invalid')
            expression = '+'.join('COALESCE(length("'+name+'"),0)' for name in names)
            total += db.execute('SELECT COALESCE(SUM('+expression+'),0) FROM "'+table+'"').fetchone()[0]  # nosec B608
            require(total <= BYTE_LIMIT, 'evolution_byte_quota')
        require(db.execute('PRAGMA page_count').fetchone()[0] <= PAGE_LIMIT, 'evolution_page_quota')
        return total

    @contextmanager
    def _transaction(self):
        with self.db.transaction() as db:
            self._usage(db)
            yield db
            self._usage(db)

    def _active(self, db, artifact_id, states=('PROMOTED',)):
        now = self._clock(db)
        row = db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()
        life = db.execute('SELECT * FROM evolution_lifecycle WHERE artifact_id=?', (artifact_id,)).fetchone()
        require(row is not None and life is not None, 'artifact_missing')
        require(row['state'] in states and life['expires_at'] > now, 'artifact_inactive_or_expired')
        require(hashlib.sha256(row['body_json']).hexdigest() == row['body_sha256'], 'artifact_body_corrupt')
        linked = json.loads(row['evidence_json'])
        for eid in linked:
            evidence = db.execute('SELECT outcome,task_family FROM evidence WHERE evidence_id=?', (eid,)).fetchone()
            require(evidence is not None and evidence['task_family'] == row['task_family']
                    and evidence['outcome'] == 'PASS', 'artifact_evidence_invalid')
        return row, life

    def _propose(self, artifact_id, kind, task_family, body, evidence_ids):
        # Core insert and lifecycle must commit together: the outer transaction
        # below performs the original bounded validations directly.
        ident(artifact_id); ident(task_family)
        require(type(evidence_ids) is list and 1 <= len(evidence_ids) <= 64
                and all(type(x) is str for x in evidence_ids)
                and len(set(evidence_ids)) == len(evidence_ids), 'artifact_evidence_invalid')
        raw, links = canonical(body, 65536), canonical(sorted(evidence_ids))
        with self._transaction() as db:
            now = self._clock(db)
            for eid in evidence_ids:
                ident(eid)
                e = db.execute('SELECT task_family,outcome FROM evidence WHERE evidence_id=?', (eid,)).fetchone()
                require(e is not None and e['task_family'] == task_family and e['outcome'] == 'PASS', 'artifact_evidence_invalid')
            prior = db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()
            if prior is not None:
                require(prior['body_json'] == raw and prior['evidence_json'] == links
                        and prior['kind'] == kind and prior['task_family'] == task_family, 'artifact_identity_conflict')
                return self._artifact(prior)
            db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,NULL)',
                (artifact_id, kind, task_family, 'CANDIDATE', raw, digest(body), links, now, now))
            ordinal = db.execute('SELECT COALESCE(MAX(ordinal),0) FROM evolution_datasets').fetchone()[0]
            db.execute('INSERT INTO evolution_lifecycle VALUES(?,?,?)',
                       (artifact_id, now + body['ttl_seconds'], ordinal))
            return self._artifact(db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (artifact_id,)).fetchone())

    @staticmethod
    def _ttl(value):
        require(type(value) is int and 1 <= value <= TTL, 'artifact_ttl_invalid')
        return value

    def propose_lesson(self, artifact_id, task_family, statement, evidence_ids, *, applicability,
                       invalidators, rule=None, ttl_seconds=3600):
        require(type(statement) is str and 0 < len(statement) <= 8192, 'lesson_statement_invalid')
        require(type(applicability) is dict and len(applicability) <= 64, 'lesson_applicability_invalid')
        require(type(invalidators) is list and len(invalidators) <= 64, 'lesson_invalidators_invalid')
        for key in invalidators:
            ident(key)
        if rule is not None:
            validate_rule(rule)
        return self._propose(artifact_id, 'LESSON', task_family,
            {'statement': statement, 'applicability': applicability, 'invalidators': invalidators,
             'rule': rule, 'ttl_seconds': self._ttl(ttl_seconds)}, evidence_ids)

    def propose_procedure(self, artifact_id, task_family, steps, evidence_ids, *, required_state,
                          mutable_inputs, validators, rule=None, ttl_seconds=3600):
        require(type(steps) is list and 1 <= len(steps) <= 64, 'procedure_steps_invalid')
        require(type(required_state) is dict, 'procedure_state_invalid')
        require(type(mutable_inputs) is list and len(mutable_inputs) <= 64
                and all(type(x) is str for x in mutable_inputs)
                and len(set(mutable_inputs)) == len(mutable_inputs), 'procedure_inputs_invalid')
        for x in mutable_inputs: ident(x)
        require(type(validators) is list and 1 <= len(validators) <= 64, 'procedure_validators_invalid')
        for x in validators: sha(x)
        if rule is not None: validate_rule(rule)
        return self._propose(artifact_id, 'PROCEDURE', task_family,
            {'steps': steps, 'required_state': required_state, 'mutable_inputs': mutable_inputs,
             'validators': validators, 'rule': rule, 'ttl_seconds': self._ttl(ttl_seconds)}, evidence_ids)

    def retrieve(self, task_family, context, *, limit=8):
        ident(task_family)
        require(type(context) is dict and type(limit) is int and 1 <= limit <= 32, 'retrieval_input_invalid')
        canonical(context)
        require(type(context.get('invalidators', [])) is list, 'context_invalidators_invalid')
        with self._transaction() as db:
            now = self._clock(db)
            rows = db.execute("SELECT * FROM artifacts WHERE task_family=? AND kind='LESSON' AND state='PROMOTED' ORDER BY updated_at DESC,artifact_id", (task_family,)).fetchall()
            lessons = []
            for row in rows:
                life = db.execute('SELECT expires_at FROM evolution_lifecycle WHERE artifact_id=?', (row['artifact_id'],)).fetchone()
                if life is None or life[0] <= now: continue
                self._active(db, row['artifact_id'])
                body = json.loads(row['body_json'])
                applicable = all(k in context and canonical(context[k]) == canonical(v)
                                 for k, v in body['applicability'].items())
                invalidated = any(context.get(k) is True or k in context.get('invalidators', [])
                                  for k in body['invalidators'])
                if applicable and not invalidated:
                    lessons.append(self._artifact(row))
                if len(lessons) == limit: break
        return {'task_family': task_family, 'lessons': lessons, 'context_sha256': digest(context),
                'execution_authorized': False}

    def register_dataset(self, dataset):
        dataset = validate_dataset(dataset)
        raw, key = canonical(dataset, 1048576), dataset_digest(dataset)
        split = dataset['split']
        require(split in ('held_out', 'development'), 'dataset_split_invalid')
        fingerprints = sorted({digest(c['subject']) for c in dataset['cases']})
        # Identical subjects are one case, regardless of renamed case IDs.
        require(len(fingerprints) == len(dataset['cases']), 'dataset_duplicate_subject')
        with self._transaction() as db:
            self._clock(db)
            prior = db.execute('SELECT document FROM evolution_datasets WHERE dataset_sha256=?', (key,)).fetchone()
            if prior is not None:
                require(prior[0] == raw, 'dataset_identity_conflict')
                return key
            for value in fingerprints:
                prior = db.execute('SELECT split FROM evolution_subjects WHERE subject_sha256=?', (value,)).fetchone()
                require(prior is None, 'dataset_partition_leak_or_reuse')
                db.execute('INSERT OR IGNORE INTO evolution_subjects VALUES(?,?)', (value, split))
            ordinal = db.execute('SELECT COALESCE(MAX(ordinal),0)+1 FROM evolution_datasets').fetchone()[0]
            db.execute('INSERT INTO evolution_datasets VALUES(?,?,?,?,NULL)', (key, split, raw, ordinal))
        return key

    def runners(self, artifact_id):
        with self._transaction() as db:
            row, _ = self._active(db, artifact_id, ('CANDIDATE', 'SIMULATION', 'PROMOTED'))
            body = json.loads(row['body_json'])
            validate_rule(body.get('rule'))
            if row['kind'] == 'PROCEDURE':
                require(body['steps'] == [{'operation':'evaluate_rule'}], 'unsupported_procedure_interpreter')
        return Runner(baseline_runner, {'verdict': 'PASS'}), Runner(artifact_runner,
            {'artifact': body, 'artifact_body_sha256': row['body_sha256']})

    def freeze(self, artifact_id, dataset, adjudications, *, repeats=3):
        baseline, candidate = self.runners(artifact_id)
        plan = freeze_plan(dataset, baseline=baseline, candidate=candidate,
                           adjudications=adjudications, repeats=repeats)
        return plan, baseline, candidate

    def _experiment(self, evaluation_id, artifact_id, plan, dataset, baseline, candidate, validator):
        ident(evaluation_id); ident(artifact_id)
        expected_baseline, expected_candidate = self.runners(artifact_id)
        require(type(candidate) is Runner and candidate.callback is artifact_runner
                and candidate.pin() == expected_candidate.pin()
                and canonical(candidate.config) == canonical(expected_candidate.config), 'candidate_not_bound_to_artifact')
        require(type(baseline) is Runner and baseline.callback is baseline_runner
                and baseline.pin() == expected_baseline.pin(), 'baseline_not_supported')
        require(plan['repeats'] >= 3 and plan['thresholds']['min_clusters'] >= 20
                and plan['thresholds']['min_accuracy_gain'] >= 0
                and plan['thresholds']['max_false_pass_rate'] == 0
                and plan['thresholds']['max_error_rate'] == 0
                and plan['thresholds']['max_instability_rate'] == 0
                and plan['thresholds']['alpha'] <= .05, 'qualification_gates_weakened')
        key = dataset_digest(validate_dataset(dataset))
        binding = digest({'artifact_id':artifact_id, 'artifact': expected_candidate.config, 'plan': plan})
        with self._transaction() as db:
            row, life = self._active(db, artifact_id, ('CANDIDATE', 'SIMULATION', 'PROMOTED'))
            saved = db.execute('SELECT * FROM evolution_datasets WHERE dataset_sha256=?', (key,)).fetchone()
            require(saved is not None and saved['split'] == 'held_out'
                    and saved['ordinal'] <= life['revision'], 'holdout_not_frozen_before_artifact')
            require(saved['used_by'] in (None, binding), 'holdout_already_consumed')
            prior = db.execute('SELECT * FROM evolution_trials WHERE evaluation_id=?', (evaluation_id,)).fetchone()
            if prior is not None:
                receipt = json.loads(prior['receipt'])
                require(prior['artifact_id'] == artifact_id and receipt['binding'] == binding, 'evaluation_identity_conflict')
                return receipt
            db.execute('UPDATE evolution_datasets SET used_by=? WHERE dataset_sha256=?', (binding, key))
        attested = callable(validator) and validator(clone(plan), clone(dataset,1048576)) is True
        job = {'plan': plan, 'dataset': dataset, 'baseline': baseline.config,
               'candidate': candidate.config, 'attested': attested}
        encoded = canonical(job, 1048576)
        # Nonblocking per-home lock prevents concurrent evaluation workers.
        fd = os.open(self.home / 'evaluation.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a+b') as lock:
            _private(self.home / 'evaluation.lock')
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise ValueError('evaluation_worker_busy') from None
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                try:
                    process = subprocess.run([sys.executable, '-B', '-m', 'keel_evolve.worker'],
                        input=encoded, stdout=output, stderr=errors,
                        cwd=Path(__file__).resolve().parents[1], timeout=8)
                except subprocess.TimeoutExpired:
                    raise ValueError('evaluation_wall_deadline') from None
                require(process.returncode == 0, 'evaluation_worker_failed')
                output.seek(0); raw = output.read(2*1024*1024+1)
                require(len(raw) <= 2*1024*1024, 'evaluation_output_limit')
                report = json.loads(raw)
        # Recompute metrics from actual trial records against the exact frozen plan.
        checked = evaluate_trials(plan, dataset, report['trials'])
        require(report['metrics'] == checked['metrics'] and report['plan_sha256'] == plan['plan_sha256'], 'worker_report_mismatch')
        by_trial={(t['case_id'],t['repeat'],t['fault'],t['runner']):t['verdict'] for t in report['trials']}
        regressions=0
        for case in dataset['cases']:
            for repeat in range(plan['repeats']):
                for fault in plan['faults']:
                    expected='ABSTAIN' if fault=='missing_evidence' else case['expected_verdict']
                    key=(case['case_id'],repeat,fault)
                    regressions += (by_trial[key+('baseline',)]==expected and by_trial[key+('candidate',)]!=expected)
        receipt = {'regressions':regressions, 'binding': binding, 'artifact_body_sha256': row['body_sha256'],
                   'plan': plan, 'report': report, 'limits': {'wall_seconds':8, 'cpu_seconds':3,
                   'address_space_bytes':512*1024*1024, 'output_bytes':2*1024*1024}}
        with self._transaction() as db:
            current, _ = self._active(db, artifact_id, ('CANDIDATE','SIMULATION','PROMOTED'))
            require(current['body_sha256'] == row['body_sha256'], 'artifact_changed_during_trial')
            db.execute('INSERT INTO evolution_trials VALUES(?,?,?,?)',
                       (evaluation_id, artifact_id, row['body_sha256'], canonical(receipt,1048576)))
        return receipt

    def qualify(self, evaluation_id, artifact_id, plan, dataset, *, baseline, candidate, adjudication_validator):
        receipt = self._experiment(evaluation_id, artifact_id, plan, dataset, baseline, candidate, adjudication_validator)
        report = receipt['report']
        require(report['status'] == 'QUALIFIED' and not report['synthetic']
                and report['adjudication_and_independence_attested'] and receipt['regressions']==0, 'evolution_reliability_not_qualified')
        with self._transaction() as db:
            row, _ = self._active(db, artifact_id, ('CANDIDATE','SIMULATION','PROMOTED'))
            require(row['body_sha256'] == receipt['artifact_body_sha256'], 'evaluation_artifact_revision_mismatch')
            db.execute("UPDATE artifacts SET state='PROMOTED',updated_at=? WHERE artifact_id=?", (self._clock(db), artifact_id))
        return {'state':'PROMOTED','artifact_id':artifact_id,'evaluation_id':evaluation_id,
                'production_qualified':False, 'qualified_task_distribution':True, 'qualified_behavior':'closed_rule_interpreter',
                'execution_authorized':False, 'receipt_sha256':hashlib.sha256(canonical(receipt,1048576)).hexdigest()}

    def tournament(self, artifact_id, cases=None, *, harness_sha256=None, repeats=3,
                   evaluation_id=None, dataset=None, plan=None):
        require(cases is None and dataset is not None and plan is not None and evaluation_id is not None,
                'declared_results_not_executed_trials')
        baseline, candidate = self.runners(artifact_id)
        receipt = self._experiment(evaluation_id, artifact_id, plan, dataset, baseline, candidate, None)
        return {'evaluation_id':evaluation_id, 'binding':receipt['binding'],
                'artifact_body_sha256':receipt['artifact_body_sha256']}

    def evaluate(self, evaluation_id, artifact_id, receipt):
        with self._transaction() as db:
            row, _ = self._active(db, artifact_id, ('CANDIDATE','SIMULATION'))
            stored = db.execute('SELECT * FROM evolution_trials WHERE evaluation_id=?', (evaluation_id,)).fetchone()
            require(stored is not None and stored['artifact_id'] == artifact_id, 'observed_trials_required')
            saved = json.loads(stored['receipt'])
            require(receipt == {'evaluation_id':evaluation_id, 'binding':saved['binding'],
                'artifact_body_sha256':saved['artifact_body_sha256']}, 'trial_receipt_mismatch')
            report = saved['report']; metrics = report['metrics']
            passed = (saved['regressions']==0 and report['gain_lower_bound'] > 0 and metrics['candidate']['false_pass'] == 0
                      and metrics['candidate']['errors'] == 0 and metrics['candidate']['instability_rate'] == 0)
            if passed:
                db.execute("UPDATE artifacts SET state='SIMULATION' WHERE artifact_id=?", (artifact_id,))
        return {'state':'SIMULATION' if passed else row['state'], 'decision':'SIMULATE' if passed else 'REJECT',
                'production_qualified':False, 'execution_authorized':False}

    def replay_plan(self, artifact_id, current_state, inputs):
        ident(artifact_id)
        require(type(inputs) is dict, 'replay_inputs_invalid')
        canonical(current_state); canonical(inputs)
        with self._transaction() as db:
            row, life = self._active(db, artifact_id, ('SIMULATION','PROMOTED'))
            require(row['kind'] == 'PROCEDURE', 'replay_procedure_missing')
            body = json.loads(row['body_json'])
            require(canonical(current_state) == canonical(body['required_state']), 'replay_precondition_mismatch')
            require(set(inputs) == set(body['mutable_inputs']), 'replay_input_binding_mismatch')
            return {'schema':'keel.evolution.replay-plan.v2','artifact_id':artifact_id,
                'artifact_body_sha256':row['body_sha256'],'steps':clone(body['steps']),
                'inputs':clone(inputs),'required_state':clone(current_state),'validators':body['validators'],
                'expires_at':life['expires_at'],'simulation_only':row['state']=='SIMULATION',
                'requires_fresh_authority':True,'execution_authorized':False}

    def bind_action_contract(self, replay_plan, *, target, evidence_bindings, authority_revision_sha256):
        require(type(replay_plan) is dict, 'action_contract_plan_invalid')
        expected = self.replay_plan(replay_plan['artifact_id'], replay_plan['required_state'], replay_plan['inputs'])
        require(canonical(expected) == canonical(replay_plan), 'action_contract_plan_changed')
        sha(authority_revision_sha256)
        require(type(evidence_bindings) is dict and 1 <= len(evidence_bindings) <= 128, 'action_contract_evidence_invalid')
        for name, value in evidence_bindings.items(): ident(name); sha(value)
        body = {'schema':'keel.evolution.action-contract.v2','plan':expected,
            'target':clone(target),'target_sha256':digest(target),
            'evidence_bindings':clone(evidence_bindings),'authority_revision_sha256':authority_revision_sha256,
            'simulation_only':expected['simulation_only'],'execution_authorized':False}
        return {**body,'contract_sha256':digest(body)}

    def validate_action_contract(self, contract, *, current_target, current_evidence_bindings,
                                 current_authority_revision_sha256, current_state=None):
        fields = {'schema','plan','target','target_sha256','evidence_bindings','authority_revision_sha256',
                  'simulation_only','execution_authorized','contract_sha256'}
        require(type(contract) is dict and set(contract) == fields
                and contract['schema']=='keel.evolution.action-contract.v2'
                and contract['execution_authorized'] is False, 'action_contract_invalid')
        body = {k:v for k,v in contract.items() if k!='contract_sha256'}
        require(digest(body)==contract['contract_sha256'] and digest(contract['target'])==contract['target_sha256'],
                'action_contract_digest_invalid')
        sha(current_authority_revision_sha256);canonical(current_evidence_bindings)
        plan = contract['plan']
        current = False
        try:
            expected = self.replay_plan(plan['artifact_id'], plan['required_state'], plan['inputs'])
            current = (current_state is not None and canonical(current_state)==canonical(plan['required_state'])
                and canonical(plan)==canonical(expected)
                and type(contract['simulation_only']) is bool
                and contract['simulation_only']==expected['simulation_only']
                and contract['target_sha256']==digest(current_target)
                and canonical(contract['evidence_bindings'])==canonical(current_evidence_bindings)
                and contract['authority_revision_sha256']==current_authority_revision_sha256)
        except ValueError:
            pass
        return {'bindings_current':current, 'simulation_only':contract['simulation_only'],
            'status':('SIMULATION_ONLY' if contract['simulation_only'] else 'CURRENT_REQUIRES_ACTION_GATE') if current else 'STALE',
            'execution_authorized':False}

    def execute_review(self, contract, *, current_target, current_evidence_bindings,
                       current_authority_revision_sha256, current_state):
        """Execute only the closed, side-effect-free rule; never grant action authority."""
        check = self.validate_action_contract(contract, current_target=current_target,
            current_evidence_bindings=current_evidence_bindings,
            current_authority_revision_sha256=current_authority_revision_sha256,
            current_state=current_state)
        require(check['bindings_current'], 'review_contract_stale')
        plan = contract['plan']
        # Serialize the last revocation check and pure interpretation with withdrawal.
        with self._transaction() as db:
            row, life = self._active(db, plan['artifact_id'], ('SIMULATION','PROMOTED'))
            require(row['body_sha256']==plan['artifact_body_sha256']
                    and (row['state']=='SIMULATION')==plan['simulation_only']
                    and life['expires_at']==plan['expires_at'], 'review_contract_stale')
            body=json.loads(row['body_json'])
            require(set(plan['inputs'])=={'subject'}, 'review_subject_required')
            result=artifact_runner(plan['inputs']['subject'],
                {'artifact':body,'artifact_body_sha256':row['body_sha256']},0)
            return {'result':result,'simulation_only':plan['simulation_only'],
                    'artifact_body_sha256':row['body_sha256'],'execution_authorized':False}

    def feedback(self, feedback_id, artifact_id, evidence_id, *, reason='contradiction'):
        ident(feedback_id);ident(artifact_id);ident(evidence_id)
        require(reason in ('contradiction','drift','runtime_failure'), 'feedback_reason_invalid')
        with self._transaction() as db:
            now = self._clock(db)
            row = db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()
            evidence = db.execute('SELECT * FROM evidence WHERE evidence_id=?', (evidence_id,)).fetchone()
            require(row is not None and evidence is not None and evidence['task_family']==row['task_family']
                    and evidence['outcome'] in ('FAIL','UNKNOWN'), 'feedback_evidence_invalid')
            prior = db.execute('SELECT * FROM evolution_feedback WHERE feedback_id=?', (feedback_id,)).fetchone()
            if prior:
                require(tuple(prior)==(feedback_id,artifact_id,evidence_id,reason), 'feedback_identity_conflict')
            else: db.execute('INSERT INTO evolution_feedback VALUES(?,?,?,?)',(feedback_id,artifact_id,evidence_id,reason))
            db.execute("UPDATE artifacts SET state='HELD',reason=?,updated_at=? WHERE artifact_id=?", (reason,now,artifact_id))
        return {'artifact_id':artifact_id,'state':'HELD','reason':reason,'execution_authorized':False}

    def rollback(self, failed_artifact_id, previous_artifact_id):
        ident(failed_artifact_id);ident(previous_artifact_id)
        require(failed_artifact_id != previous_artifact_id, 'rollback_same_artifact')
        with self._transaction() as db:
            previous, _ = self._active(db, previous_artifact_id)
            failed = db.execute('SELECT * FROM artifacts WHERE artifact_id=?',(failed_artifact_id,)).fetchone()
            require(failed is not None and failed['kind']==previous['kind']
                    and failed['task_family']==previous['task_family']
                    and previous['created_at'] <= failed['created_at'], 'rollback_scope_invalid')
            # Selection cannot restore a retired or expired version, or clear a hold.
            db.execute("UPDATE artifacts SET state='HELD',reason='rollback',updated_at=? WHERE artifact_id=?",
                       (self._clock(db),failed_artifact_id))
            return {'selected_artifact_id':previous_artifact_id,'failed_artifact_id':failed_artifact_id,
                    'execution_authorized':False}

    def backup(self, destination):
        destination = Path(os.path.abspath(destination))
        require(destination.parent.resolve(strict=True)==destination.parent,'backup_parent_invalid')
        destination.mkdir(mode=0o700,exist_ok=False)
        target=destination/'evolution.sqlite3'
        descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(descriptor)
        # A separate read connection backs up the committed snapshot while the
        # private transaction excludes concurrent writers.
        with self._transaction() as db:
            self._clock(db)
            with sqlite3.connect(self.db.path.as_uri()+'?mode=ro',uri=True) as source:
                with sqlite3.connect(target) as output:
                    source.backup(output)
        fd=os.open(target,os.O_RDONLY|os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)
        body={'schema':'keel.evolution.backup.v1','sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
              'restore_policy':'hold_all_artifacts_pending_requalification'}
        path=destination/'backup.json'
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream: stream.write(canonical(body));stream.flush();os.fsync(stream.fileno())
        for parent in (destination,destination.parent):
            fd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(fd)
            finally:os.close(fd)
        return body

    @classmethod
    def restore(cls, backup_home, destination, *, clock=time.time):
        import shutil
        backup_home=Path(os.path.abspath(backup_home));_private(backup_home,True)
        source=backup_home/'evolution.sqlite3';_private(source);_private(backup_home/'backup.json')
        require(source.stat().st_size <= PAGE_LIMIT*4096,'backup_too_large')
        metadata=json.loads((backup_home/'backup.json').read_bytes())
        require(metadata=={'schema':'keel.evolution.backup.v1',
                'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
                'restore_policy':'hold_all_artifacts_pending_requalification'},'backup_digest_invalid')
        destination=Path(os.path.abspath(destination))
        require(destination.parent.resolve(strict=True)==destination.parent,'restore_parent_invalid')
        from keel_machine.common import _ancestors
        _ancestors(destination)
        destination.mkdir(mode=0o700,exist_ok=False)
        target=destination/'evolution.sqlite3'
        with target.open('xb') as output,source.open('rb') as input_stream:
            os.chmod(target,0o600);shutil.copyfileobj(input_stream,output);output.flush();os.fsync(output.fileno())
        restored=cls(destination,clock=clock)
        with restored._transaction() as db:
            require(db.execute('PRAGMA integrity_check').fetchone()[0]=='ok','restore_integrity_failed')
            restored._clock(db)
            db.execute("UPDATE artifacts SET state='HELD',reason='restored_requires_requalification'")
        restored._sync_home()
        return restored
