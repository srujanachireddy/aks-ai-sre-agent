from pathlib import Path

import pytest

from sre_agent.evals import Scenario

SCENARIO_DIR = Path(__file__).resolve().parents[1] / "evals" / "scenarios"


@pytest.fixture
def scenarios() -> dict[str, Scenario]:
    return {s.name: s for s in (Scenario.load(p) for p in sorted(SCENARIO_DIR.glob("*.yaml")))}
