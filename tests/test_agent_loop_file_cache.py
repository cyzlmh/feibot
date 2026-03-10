from pathlib import Path

import pytest

from feibot.agent.loop import AgentLoop
from feibot.bus.events import InboundMessage
from feibot.bus.queue import MessageBus
from feibot.providers.base import LLMProvider, LLMResponse


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


@pytest.mark.asyncio
async def test_file_message_is_cached_until_followup_text(monkeypatch, tmp_path):
    loop = _make_loop(tmp_path)
    captured: dict[str, str] = {}

    async def _fake_run_agent_loop(
        initial_messages,
        user_goal,
        debug_log=None,
        on_progress=None,
        disabled_tools=None,
    ):
        captured["user_goal"] = user_goal
        return "done", [], {"stopped_reason": "stop", "history_messages": []}

    monkeypatch.setattr(loop, "_run_agent_loop", _fake_run_agent_loop)

    file_msg = InboundMessage(
        channel="feishu",
        sender_id="ou_test",
        chat_id="ou_test",
        content="[file: /tmp/report.pdf]",
        media=["/tmp/report.pdf"],
        metadata={"msg_type": "file", "message_id": "om_file_1"},
    )
    ack = await loop._process_message(file_msg)

    assert ack is not None
    assert "已收到文件并缓存" in ack.content
    session = loop.sessions.get_or_create("feishu:ou_test")
    assert session.metadata["pending_files"] == ["/tmp/report.pdf"]

    text_msg = InboundMessage(
        channel="feishu",
        sender_id="ou_test",
        chat_id="ou_test",
        content="请总结这篇论文",
        metadata={"msg_type": "text", "message_id": "om_text_1"},
    )
    resp = await loop._process_message(text_msg)

    assert resp is not None
    assert resp.content == "done"
    assert "请总结这篇论文" in captured["user_goal"]
    assert "[file: /tmp/report.pdf]" in captured["user_goal"]
    session = loop.sessions.get_or_create("feishu:ou_test")
    assert "pending_files" not in session.metadata


@pytest.mark.asyncio
async def test_new_clears_pending_files_from_session_metadata(tmp_path):
    loop = _make_loop(tmp_path)

    file_msg = InboundMessage(
        channel="feishu",
        sender_id="ou_test",
        chat_id="ou_test",
        content="[file: /tmp/report.pdf]",
        media=["/tmp/report.pdf"],
        metadata={"msg_type": "file", "message_id": "om_file_1"},
    )
    ack = await loop._process_message(file_msg)
    assert ack is not None
    old_session_id = loop.sessions.get_or_create("feishu:ou_test").session_id

    new_msg = InboundMessage(
        channel="feishu",
        sender_id="ou_test",
        chat_id="ou_test",
        content="/new",
        metadata={"msg_type": "text", "message_id": "om_new_1"},
    )
    resp = await loop._process_message(new_msg)

    assert resp is not None
    assert resp.content == "New session started."
    session = loop.sessions.get_or_create("feishu:ou_test")
    assert session.session_id != old_session_id
    assert "pending_files" not in session.metadata


@pytest.mark.asyncio
async def test_new_command_with_mention_prefix_resets_session(tmp_path):
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("feishu:oc_group_1")
    session.add_message("user", "old context")
    loop.sessions.save(session)
    old_session_id = session.session_id

    resp = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_test",
            chat_id="oc_group_1",
            content="@_user_1 /new",
            metadata={"msg_type": "text", "message_id": "om_new_mention_1"},
        )
    )

    assert resp is not None
    assert resp.content == "New session started."
    session = loop.sessions.get_or_create("feishu:oc_group_1")
    assert session.session_id != old_session_id
    assert session.messages == []


@pytest.mark.asyncio
async def test_chatid_command_returns_sender_and_chat_ids(tmp_path):
    loop = _make_loop(tmp_path)

    resp = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_test",
            chat_id="oc_group_1",
            content="@_user_1 /chatid",
            metadata={"msg_type": "text", "message_id": "om_chatid_1"},
        )
    )

    assert resp is not None
    assert "ou_test" in resp.content
    assert "oc_group_1" in resp.content
