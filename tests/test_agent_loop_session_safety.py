from pathlib import Path

import pytest

from feibot.agent.loop import AgentLoop
from feibot.bus.events import InboundMessage
from feibot.bus.queue import MessageBus
from feibot.providers.base import LLMProvider, LLMResponse
from feibot.session.manager import Session


class DummyProvider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        return LLMResponse(content="unused")

    def get_default_model(self) -> str:
        return "dummy/test-model"


class NoToolsAgentLoop(AgentLoop):
    def _register_default_tools(self) -> None:  # pragma: no cover - test helper
        return None


def _make_loop(tmp_path: Path) -> AgentLoop:
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    return NoToolsAgentLoop(
        bus=MessageBus(),
        provider=DummyProvider(),
        workspace=workspace,
        model="dummy/test-model",
        memory_window=20,
    )


def test_append_session_history_skips_empty_assistant_without_tool_calls(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    session = Session(key="cli:direct")

    loop._append_session_history(
        session,
        [
            {"role": "assistant", "content": ""},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "function": {"name": "exec", "arguments": "{}"}}],
            },
            {"role": "assistant", "content": "done"},
        ],
    )

    assert len(session.messages) == 2
    assert session.messages[0].get("tool_calls")
    assert session.messages[1]["content"] == "done"


@pytest.mark.asyncio
async def test_llm_error_response_not_persisted_to_session(monkeypatch, tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)

    async def _fake_run_agent_loop(
        initial_messages,
        user_goal,
        debug_log=None,
        on_progress=None,
        disabled_tools=None,
    ):
        return "provider error", [], {"stopped_reason": "llm_error", "history_messages": []}

    monkeypatch.setattr(loop, "_run_agent_loop", _fake_run_agent_loop)

    resp = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_test",
            chat_id="ou_test",
            content="hello",
            metadata={"msg_type": "text", "message_id": "om_test_1"},
        )
    )

    assert resp is not None
    assert resp.content == "provider error"
    session = loop.sessions.get_or_create("feishu:ou_test")
    assert [m["role"] for m in session.messages] == ["user"]
