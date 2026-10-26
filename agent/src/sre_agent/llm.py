"""Chat model adapters.

`ChatModel.complete` takes OpenAI-format messages and tool specs and returns one
assistant turn. Two implementations:
- OpenAIChatModel: Azure OpenAI (or any OpenAI-compatible endpoint), configured by env vars.
- ScriptedModel: replays a fixed list of turns, so the agent loop can be unit-tested offline.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Turn:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ChatModel(Protocol):
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Turn: ...


def to_openai_tools(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": spec} for spec in specs]


class OpenAIChatModel:
    """Azure OpenAI when AZURE_OPENAI_ENDPOINT is set, otherwise any OpenAI-compatible API.

    Azure:  AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT, and either AZURE_OPENAI_API_KEY
            or managed identity (workload identity in AKS: no key at all).
    Other:  OPENAI_BASE_URL (optional), OPENAI_API_KEY, OPENAI_MODEL.
    """

    def __init__(self) -> None:
        from openai import AzureOpenAI, OpenAI

        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        if endpoint:
            self.model = os.environ["AZURE_OPENAI_DEPLOYMENT"]
            api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
            if os.getenv("AZURE_OPENAI_API_KEY"):
                self.client = AzureOpenAI(azure_endpoint=endpoint, api_version=api_version,
                                          api_key=os.environ["AZURE_OPENAI_API_KEY"])
            else:
                from azure.identity import DefaultAzureCredential, get_bearer_token_provider

                token = get_bearer_token_provider(DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
                self.client = AzureOpenAI(azure_endpoint=endpoint, api_version=api_version, azure_ad_token_provider=token)
        else:
            self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            self.client = OpenAI(base_url=os.getenv("OPENAI_BASE_URL") or None)

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Turn:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=to_openai_tools(tools),
            tool_choice="required",  # every turn must call a tool; the last one is submit_diagnosis
            temperature=0,
        )
        msg = resp.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable": tc.function.arguments}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
        usage = resp.usage
        return Turn(
            content=msg.content,
            tool_calls=calls,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )


class ScriptedModel:
    """Returns pre-written turns in order. For tests: proves the loop, not the model."""

    def __init__(self, turns: list[Turn]):
        self._turns = list(turns)
        self.seen_messages: list[list[dict[str, Any]]] = []

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Turn:
        self.seen_messages.append(list(messages))
        if not self._turns:
            return Turn(content="I have nothing more to add.")
        return self._turns.pop(0)
