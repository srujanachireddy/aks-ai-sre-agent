"""Pins down what the rule-based baseline gets right and wrong, so the comparison stays honest."""

from sre_agent.baseline import triage
from sre_agent.evals import report, run, score
from sre_agent.tools import FixtureBackend

from .conftest import SCENARIO_DIR


def results(scenarios):
    return {name: score(s, triage(s.alert, FixtureBackend(s.responses)), "baseline") for name, s in scenarios.items()}


def test_baseline_strengths(scenarios):
    r = results(scenarios)
    assert r["bad_deploy"].correct  # obvious deploy regression
    assert r["dependency_failure"].correct  # obvious connection errors


def test_baseline_weaknesses(scenarios):
    r = results(scenarios)
    # Rules can classify these but can't choose a safe new value, so they escalate.
    assert r["memory_exhaustion"].category_ok and not r["memory_exhaustion"].action_ok
    assert r["capacity"].category_ok and not r["capacity"].action_ok
    # The trap: a coincidental deploy gets blamed and rolled back.
    assert r["misleading_deploy"].category == "bad_deploy" and not r["misleading_deploy"].correct


def test_eval_report_runs_without_api_key():
    out = report(run(SCENARIO_DIR, use_agent=False, runs=1))
    assert "baseline: 2/5 correct (40%)" in out
