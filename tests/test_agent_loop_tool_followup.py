import copy
from pathlib import Path
from typing import Any

import pytest

from feibot.agent.loop import AgentLoop
from feibot.agent.tools.base import Tool
from feibot.bus.queue import MessageBus
from feibot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class _DummyTool(Tool):
    @property
    def name(self) -> str:
        return "dummy_tool"

    @property
    def description(self) -> str:
        return "Return a deterministic echo payload for tests."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }

    async def execute(self, **kwargs: Any) -> str:
        return f"dummy:{kwargs['value']}"


class _TwoStepProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[dict[str, Any]]] = []
        self.call_count = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls.append(copy.deepcopy(messages))
        self.call_count += 1
        if self.call_count == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call_dummy_1",
                        name="dummy_tool",
                        arguments={"value": "alpha"},
                    )
                ],
                finish_reason="tool_calls",
            )
        if self.call_count == 2:
            return LLMResponse(content="done", tool_calls=[])
        raise AssertionError("Provider should only be called twice in this regression test.")

    def get_default_model(self) -> str:
        return "dummy/test-model"


class _ToolOnlyAgentLoop(AgentLoop):
    def _register_default_tools(self) -> None:  # pragma: no cover - test helper
        self.tools.register(_DummyTool())


def _make_loop(tmp_path: Path, provider: LLMProvider) -> AgentLoop:
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    return _ToolOnlyAgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=workspace,
        model="dummy/test-model",
        max_iterations=5,
    )


@pytest.mark.asyncio
async def test_tool_followup_uses_tool_result_as_next_input_tail(tmp_path: Path) -> None:
    provider = _TwoStepProvider()
    loop = _make_loop(tmp_path, provider=provider)
    initial_messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "run dummy tool"},
    ]

    final_content, tools_used, loop_meta = await loop._run_agent_loop(
        initial_messages,
        user_goal="run dummy tool",
    )

    assert final_content == "done"
    assert tools_used == ["dummy_tool"]
    assert loop_meta.get("stopped_reason") == "completed"
    assert provider.call_count == 2

    second_call_messages = provider.calls[1]
    assert second_call_messages[-1]["role"] == "tool"
    assert second_call_messages[-1]["tool_call_id"] == "call_dummy_1"
    assert second_call_messages[-1]["name"] == "dummy_tool"
    assert second_call_messages[-1]["content"] == "dummy:alpha"
    assert not any(
        m.get("role") == "user"
        and m.get("content") == "Reflect on the results and decide next steps."
        for m in second_call_messages
    )

