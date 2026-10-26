"""The agent loop with a scripted model: proves tool dispatch, validation and safety, not model quality."""

from sre_agent.agent import investigate
from sre_agent.llm import ScriptedModel, ToolCall, Turn
from sre_agent.models import ActionType, Category
from sre_agent.tools import FixtureBackend

GOOD_SUBMIT = {
    "category": "bad_deploy",
    "root_cause": "v1.4.0 raises KeyError 'currency' in apply_discount; errors began at the 14:01 rollout.",
    "confidence": 0.9,
    "evidence": ["error rate 0.2% -> 31% after 14:01", "412 KeyError: 'currency'", "rev 7 v1.4.0 rolled out 14:01"],
    "action": {"type": "rollback", "target_image_tag": "v1.3.2", "rationale": "last healthy version"},
}


def test_agent_uses_tools_then_submits(scenarios):
    s = scenarios["bad_deploy"]
    model = ScriptedModel([
        Turn(tool_calls=[ToolCall("1", "get_service_metrics", {"service": "checkout"}),
                         ToolCall("2", "get_deploy_history", {"service": "checkout"})], prompt_tokens=900, completion_tokens=40),
        Turn(tool_calls=[ToolCall("3", "get_recent_logs", {"service": "checkout"})], prompt_tokens=1400, completion_tokens=30),
        Turn(tool_calls=[ToolCall("4", "submit_diagnosis", GOOD_SUBMIT)], prompt_tokens=1900, completion_tokens=150),
    ])
    inv = investigate(s.alert, FixtureBackend(s.responses), model)

    assert inv.diagnosis.category == Category.BAD_DEPLOY
    assert inv.diagnosis.action.target_image_tag == "v1.3.2"
    assert [t.tool for t in inv.tool_calls] == ["get_service_metrics", "get_deploy_history", "get_recent_logs"]
    assert inv.steps == 3 and inv.prompt_tokens == 4200
    # the tool results were actually fed back to the model
    last_messages = model.seen_messages[-1]
    assert any(m["role"] == "tool" and "KeyError" in m["content"] for m in last_messages)


def test_invalid_diagnosis_is_bounced_back_to_the_model(scenarios):
    s = scenarios["bad_deploy"]
    bad = dict(GOOD_SUBMIT, confidence=7)  # outside 0..1
    model = ScriptedModel([
        Turn(tool_calls=[ToolCall("1", "submit_diagnosis", bad)]),
        Turn(tool_calls=[ToolCall("2", "submit_diagnosis", GOOD_SUBMIT)]),
    ])
    inv = investigate(s.alert, FixtureBackend(s.responses), model)
    assert inv.diagnosis.confidence == 0.9
    assert any(m["role"] == "tool" and "diagnosis rejected" in m["content"] for m in model.seen_messages[-1])


def test_unknown_tool_is_harmless(scenarios):
    s = scenarios["bad_deploy"]
    model = ScriptedModel([
        Turn(tool_calls=[ToolCall("1", "kubectl_delete", {"what": "everything"})]),
        Turn(tool_calls=[ToolCall("2", "submit_diagnosis", GOOD_SUBMIT)]),
    ])
    inv = investigate(s.alert, FixtureBackend(s.responses), model)
    assert inv.diagnosis.category == Category.BAD_DEPLOY
    assert "unknown tool" in [m for m in model.seen_messages[-1] if m["role"] == "tool"][0]["content"]


def test_step_budget_exhausted_escalates(scenarios):
    s = scenarios["bad_deploy"]
    loop = [Turn(tool_calls=[ToolCall(str(i), "get_service_metrics", {"service": "checkout"})]) for i in range(20)]
    inv = investigate(s.alert, FixtureBackend(s.responses), ScriptedModel(loop), max_steps=4)
    assert inv.diagnosis.category == Category.UNKNOWN
    assert inv.diagnosis.action.type == ActionType.ESCALATE
    assert len(inv.tool_calls) == 4


def test_model_outage_escalates_instead_of_crashing(scenarios):
    class Down:
        def complete(self, messages, tools):
            raise TimeoutError("endpoint unreachable")

    inv = investigate(scenarios["bad_deploy"].alert, FixtureBackend({}), Down())
    assert inv.error and "TimeoutError" in inv.error
    assert inv.diagnosis.action.type == ActionType.ESCALATE
