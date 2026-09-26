"""Keel shadow validation lane (Workstream F).

shadow.pipeline -- seven-revision export -> revision adapter -> assurance ->
trust -> consent -> execution policy -> SHADOW result.

Hard rule: execution_authorized is False unless a COMPLETELY SEPARATE valid
execution authorization exists. Source completeness is NEVER execution
authorization, and a synthetic fixture NEVER grants consent.

Since no genuine human decision exists, the pipeline validates on
clearly-labeled SYNTHETIC fixtures (in-memory only, never persisted) and
honestly reports SHADOW_VALIDATED=false pending a genuine decision.
"""
