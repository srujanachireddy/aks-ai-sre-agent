"""Scores the agent and the rule-based baseline against recorded incident scenarios.

Usage:
  sre-agent-evals evals/scenarios                 # baseline only (free, no API key)
  sre-agent-evals evals/scenarios --agent         # + the LLM agent (needs Azure OpenAI / OpenAI env vars)
  sre-agent-evals evals/scenarios --agent --runs 3   # repeat to measure consistency

A result counts as correct only if the category is right AND the action is right
(and, for rollbacks, the exact target tag). The proposed action also has to pass guardrails.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from .agent import investigate
from .baseline import triage
from .guardrails import check
from .models import ActionType, Alert, Diagnosis
from .tools import FixtureBackend


@dataclass
class Scenario:
    name: str
    description: str
    alert: Alert
    current: dict
    expected: dict
    responses: dict

    @classmethod
    def load(cls, path: Path) -> "Scenario":
        data = yaml.safe_load(path.read_text())
        return cls(data["name"], data.get("description", ""), Alert(**data["alert"]),
                   data["current"], data["expected"], data["responses"])

    def known_tags(self) -> set[str]:
        import re

        history = str(self.responses.get("get_deploy_history", {}).get(self.alert.service, ""))
        return set(re.findall(r"tag ([A-Za-z0-9_.\-]+)", history))


@dataclass
class Score:
    scenario: str
    system: str
    category_ok: bool
    action_ok: bool
    guardrails_ok: bool
    category: str
    action: str
    steps: int = 0
    tokens: int = 0
    proposed_value: str = ""
    guardrail_checks: str = ""

    @property
    def correct(self) -> bool:
        return self.category_ok and self.action_ok and self.guardrails_ok


def score(s: Scenario, d: Diagnosis, system: str, steps: int = 0, tokens: int = 0) -> Score:
    exp_action = s.expected["action"]

    action_ok = d.action.type.value == exp_action

    if action_ok and exp_action == ActionType.ROLLBACK.value:
        action_ok = d.action.target_image_tag == s.expected.get("target_image_tag")

    guard = check(d, s.current, s.known_tags())

    guard_ok = guard.allowed or d.action.type == ActionType.ESCALATE

    # Capture the exact proposed remediation
    proposed_value = ""

    if d.action.type == ActionType.INCREASE_MEMORY:
        proposed_value = f"{d.action.memory_limit_mi} MiB"

    elif d.action.type == ActionType.SCALE:
        proposed_value = f"{d.action.replicas} replicas"

    elif d.action.type == ActionType.ROLLBACK:
        proposed_value = str(d.action.target_image_tag)

    return Score(
        scenario=s.name,
        system=system,
        category_ok=d.category.value == s.expected["category"],
        action_ok=action_ok,
        guardrails_ok=guard_ok,
        category=d.category.value,
        action=d.action.type.value + (
            f" {d.action.target_image_tag}"
            if d.action.target_image_tag else ""
        ),
        steps=steps,
        tokens=tokens,
        proposed_value=proposed_value,
        guardrail_checks="; ".join(guard.checks),
    )


def run(scenario_dir: Path, use_agent: bool, runs: int) -> list[Score]:
    scenarios = [Scenario.load(p) for p in sorted(scenario_dir.glob("*.yaml"))]
    scores: list[Score] = []
    model = None
    if use_agent:
        from .llm import OpenAIChatModel

        model = OpenAIChatModel()
    for s in scenarios:
        backend = FixtureBackend(s.responses)
        scores.append(score(s, triage(s.alert, backend), "baseline"))
        for _ in range(runs if model else 0):
            inv = investigate(s.alert, backend, model)
            assert inv.diagnosis is not None
            scores.append(score(s, inv.diagnosis, "agent", inv.steps, inv.prompt_tokens + inv.completion_tokens))
    return scores


def report(scores: list[Score]) -> str:
    lines = ["| Scenario | System | Category | Action | Correct |", "|---|---|---|---|---|"]
    for sc in scores:
        lines.append(f"| {sc.scenario} | {sc.system} | {sc.category} {'✓' if sc.category_ok else '✗'} | "
                     f"{sc.action} {'✓' if sc.action_ok else '✗'} | {'**yes**' if sc.correct else 'no'} |")
    lines.append("")
    lines.append("")
    lines.append("### Remediation and guardrail details")
    lines.append("")
    lines.append("| Scenario | System | Proposed value | Guardrail result |")
    lines.append("|---|---|---|---|")

    for sc in scores:
        checks = sc.guardrail_checks or "No checks recorded"
        lines.append(
            f"| {sc.scenario} | {sc.system} | "
            f"{sc.proposed_value or '-'} | {checks} |"
        )
    for system in sorted({sc.system for sc in scores}):
        mine = [sc for sc in scores if sc.system == system]
        correct = sum(sc.correct for sc in mine)
        extra = ""
        if system == "agent":
            extra = f", avg {sum(sc.steps for sc in mine) / len(mine):.1f} steps, avg {sum(sc.tokens for sc in mine) / len(mine):.0f} tokens"
        lines.append(f"**{system}: {correct}/{len(mine)} correct ({correct / len(mine):.0%}){extra}**  ")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("scenarios", type=Path)
    p.add_argument("--agent", action="store_true", help="also evaluate the LLM agent")
    p.add_argument("--runs", type=int, default=1, help="agent runs per scenario")
    p.add_argument("--json", type=Path, help="write raw scores to this file")
    args = p.parse_args(argv)
    scores = run(args.scenarios, args.agent, args.runs)
    print(report(scores))
    if args.json:
        args.json.write_text(json.dumps([sc.__dict__ | {"correct": sc.correct} for sc in scores], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
