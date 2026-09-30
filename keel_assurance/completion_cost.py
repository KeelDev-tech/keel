"""Offline attempt-cost accounting; supplied verifier evidence grants no authority.

Each input covers one declared task cohort, outcome definition, time window,
unit and cost basis. It never estimates prices or reads production state.
"""
from decimal import Decimal, InvalidOperation, localcontext
import re

from .metrics import MetricsError, _fields, _sequence, _text


def _amount(value):
    if value is None:
        return None
    # Decimal strings avoid binary float artifacts and accidental booleans.
    if (type(value) is not str or len(value) > 64
            or re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value) is None):
        raise MetricsError("amount: bounded decimal string or null required")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise MetricsError("amount: invalid decimal") from exc
    if not amount.is_finite() or amount < 0 or amount > Decimal('1e18'):
        raise MetricsError("amount: finite nonnegative value <= 1e18 required")
    if amount.as_tuple().exponent < -18:
        raise MetricsError("amount: at most 18 fractional places required")
    return amount


def summarize_completion_cost(payload):
    """All attempt spend / distinct verifier-positive tasks, including retries.

    Unknown outcome tasks retain spend in the numerator. Missing any amount
    suppresses the headline total and ratio; known spend stays visible.
    Verifier references are caller-supplied audit pointers, not authenticated.
    """
    _fields(payload, ('schema_version', 'cohort', 'window', 'outcome_definition',
                      'unit', 'basis', 'attempts', 'outcomes'))
    if type(payload['schema_version']) is not int or payload['schema_version'] != 1:
        raise MetricsError('unsupported schema_version')
    for key in ('cohort', 'window', 'outcome_definition', 'unit'):
        _text(payload[key], key)
    if payload['basis'] not in ('trace_estimate', 'provider_usage', 'invoice_allocation'):
        raise MetricsError('unsupported cost basis')
    outcomes = {}
    for row in _sequence(payload['outcomes'], 'outcomes'):
        _fields(row, ('task_id', 'verified', 'verifier_ref'))
        task = _text(row['task_id'], 'task_id')
        if task in outcomes:
            raise MetricsError('duplicate task outcome')
        verified, ref = row['verified'], row['verifier_ref']
        if verified is not None and type(verified) is not bool:
            raise MetricsError('verified: boolean or null required')
        if ref is not None:
            _text(ref, 'verifier_ref', maximum=2048)
        if verified is not None and ref is None:
            raise MetricsError('known outcome requires verifier_ref')
        outcomes[task] = verified
    attempts, seen = [], set()
    for row in _sequence(payload['attempts'], 'attempts'):
        _fields(row, ('task_id', 'attempt_id', 'amount'))
        task = _text(row['task_id'], 'task_id')
        identity = _text(row['attempt_id'], 'attempt_id')
        if identity in seen:
            raise MetricsError('duplicate attempt_id')
        seen.add(identity)
        if task not in outcomes:
            raise MetricsError('attempt task missing from declared outcomes')
        attempts.append((task, _amount(row['amount'])))
    if set(outcomes) != {task for task, _ in attempts}:
        raise MetricsError('each declared task requires at least one attempt')
    with localcontext() as ctx:
        ctx.prec = 60
        known = sum((amount for _, amount in attempts if amount is not None), Decimal(0))
        successful = sum((amount for task, amount in attempts
                          if outcomes[task] is True and amount is not None), Decimal(0))
        failed = sum((amount for task, amount in attempts
                      if outcomes[task] is False and amount is not None), Decimal(0))
        unknown = known - successful - failed
        missing = sum(amount is None for _, amount in attempts)
        positives = sum(value is True for value in outcomes.values())
        total = None if missing or not attempts else known
        ratio = total / positives if total is not None and positives else None
        result = {key: payload[key] for key in
                  ('schema_version', 'cohort', 'window', 'outcome_definition', 'unit', 'basis')}
        result.update({
            'execution_authority': False,
            'counts': {'tasks': len(outcomes), 'attempts': len(attempts),
                       'additional_attempts': len(attempts) - len(outcomes),
                       'verified_completions': positives,
                       'verified_failures': sum(value is False for value in outcomes.values()),
                       'unknown_outcomes': sum(value is None for value in outcomes.values()),
                       'unmetered_attempts': missing},
            'known_attempt_spend': str(known),
            'known_spend_on_verified_tasks': str(successful),
            'known_spend_on_failed_tasks': str(failed),
            'known_spend_on_unknown_tasks': str(unknown),
            'total_attempt_spend': str(total) if total is not None else None,
            'cost_per_verified_completion': str(ratio) if ratio is not None else None,
            'limitations': [
                'Caller-supplied outcome references and cost records are not authenticated.',
                'Completeness is checked only within the declared cohort; omitted attempts cannot be detected.',
                'Cost bases and units must be reconciled separately; estimates are not invoices.',
                'A verified outcome is the declared postcondition, not permission to submit or execute.',
            ],
        })
        return result
