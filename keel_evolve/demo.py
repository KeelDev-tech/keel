"""Compatibility entry point for the artifact-bound offline workflow."""
from .workflow import run as run_workflow


def run(home):
    result = run_workflow(home)
    result['status'] = ('EVOLUTION_DEMO_PASSED' if result['status'] == 'WORKFLOW_PASSED'
                        else 'EVOLUTION_DEMO_FAILED')
    return result
