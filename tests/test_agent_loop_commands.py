from pathlib import Path

import pytest

from feibot.agent.loop import AgentLoop
from feibot.bus.events import InboundMessage
from feibot.bus.queue import MessageBus
from feibot.providers.base import LLMProvider, LLMResponse


class _DummyProvider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        return LLMResponse(content="unused")

    def get_default_model(self) -> str:
        return "dummy/test-model"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_id", "content", "message_id"),
    [
        ("ou_user_1", "/go", "om_go_dm"),
        ("oc_group_1", "@_user_1 /go", "om_go_group"),
    ],
)
async def test_go_command_requires_persisted_resume_state(
    tmp_path: Path,
    chat_id: str,
    content: str,
    message_id: str,
) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_DummyProvider(),
        workspace=tmp_path,
        model="dummy/test-model",
    )

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_requester",
            chat_id=chat_id,
            content=content,
            metadata={"msg_type": "text", "message_id": message_id},
        )
    )

    assert response is not None
    assert response.content == "No paused task to resume."


@pytest.mark.asyncio
async def test_go_command_reuses_persisted_resume_state_without_prompt_injection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_DummyProvider(),
        workspace=tmp_path,
        model="dummy/test-model",
    )
    seen: dict[str, object] = {}
    session = loop.sessions.get_or_create("feishu:ou_user_resume")
    resume_messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "original request"},
        {"role": "assistant", "content": "partial progress"},
        {"role": "user", "content": AgentLoop.REFLECT_PROMPT},
    ]
    session.metadata[AgentLoop.RESUME_STATE_METADATA_KEY] = {
        "version": 1,
        "status": "paused",
        "reason": "max_iterations",
        "user_goal": "finish the work",
        "messages": resume_messages,
        "disabled_tools": ["spawn"],
    }
    loop.sessions.save(session)

    async def _fake_run_agent_loop(
        initial_messages,
        user_goal,
        debug_log=None,
        on_progress=None,
        disabled_tools=None,
        on_checkpoint=None,
    ):
        seen["initial_messages"] = initial_messages
        seen["user_goal"] = user_goal
        seen["disabled_tools"] = disabled_tools
        return "resumed", [], {"history_messages": [], "stopped_reason": "completed"}

    monkeypatch.setattr(loop, "_run_agent_loop", _fake_run_agent_loop)

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_requester",
            chat_id="ou_user_resume",
            content="/go",
            metadata={"msg_type": "text", "message_id": "om_go_resume"},
        )
    )

    assert response is not None
    assert response.content == "resumed"
    assert seen["user_goal"] == "finish the work"
    assert seen["initial_messages"] == resume_messages
    assert seen["disabled_tools"] == {"spawn"}


def test_incomplete_response_instructs_user_to_send_go(tmp_path: Path) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_DummyProvider(),
        workspace=tmp_path,
        model="dummy/test-model",
    )

    message = loop._build_incomplete_response(
        reason="too many consecutive tool errors",
        user_goal="clean temp files",
        tools_used=["exec"],
        recent_observations=["exec: approval required"],
    )

    assert "/go" in message
    assert "回复“继续”" not in message
