import pytest

from feibot.agent.loop import AgentLoop
from feibot.bus.events import InboundMessage
from feibot.bus.queue import MessageBus
from feibot.providers.base import LLMProvider, LLMResponse


class _FakeProvider(LLMProvider):
    def __init__(self, response: LLMResponse | None = None):
        super().__init__()
        self._response = response or LLMResponse(content="done")
        self.calls = 0

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
    ):
        self.calls += 1
        return self._response

    def get_default_model(self) -> str:
        return "fake-model"


@pytest.mark.asyncio
async def test_fork_command_forks_feishu_session(monkeypatch, tmp_path):
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_FakeProvider(LLMResponse(content="unused")),
        workspace=tmp_path,
        model="fake-model",
    )
    seen: dict[str, str | None] = {}

    async def _fake_open_fork_chat(
        *,
        label: str | None,
        origin_chat_id: str,
        sender_id: str,
        channel: str,
        source_session,
    ):
        seen["label"] = label
        seen["origin_chat_id"] = origin_chat_id
        seen["sender_id"] = sender_id
        seen["channel"] = channel
        seen["source_key"] = source_session.key
        return "opened"

    monkeypatch.setattr(loop, "_open_fork_chat", _fake_open_fork_chat)

    resp = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_test",
            chat_id="oc_parent_1",
            content="@_user_1 /fork quick triage",
            metadata={"msg_type": "text", "message_id": "om_fork_1"},
        )
    )

    assert resp is not None
    assert resp.content == "opened"
    assert seen == {
        "label": "quick triage",
        "origin_chat_id": "oc_parent_1",
        "sender_id": "ou_test",
        "channel": "feishu",
        "source_key": "feishu:oc_parent_1",
    }


@pytest.mark.asyncio
async def test_fork_command_rejects_non_feishu(tmp_path):
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_FakeProvider(LLMResponse(content="unused")),
        workspace=tmp_path,
        model="fake-model",
    )

    resp = await loop._process_message(
        InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="direct",
            content="/fork",
            metadata={"msg_type": "text", "message_id": "msg_fork_cli"},
        )
    )

    assert resp is not None
    assert "only supported in Feishu chats" in resp.content


def test_fork_session_context_copies_full_history_and_metadata(tmp_path):
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_FakeProvider(LLMResponse(content="unused")),
        workspace=tmp_path,
        model="fake-model",
    )

    source = loop.sessions.get_or_create("feishu:oc_source")
    source.messages = [
        {"role": "user", "content": "hello", "timestamp": "2026-03-23T10:00:00"},
        {"role": "assistant", "content": "world", "timestamp": "2026-03-23T10:00:01"},
    ]
    source.metadata = {"resume_state": {"status": "paused"}, "x": [1, 2, 3]}
    loop.sessions.save(source)

    loop._fork_session_context(
        source_session=source,
        target_session_key="feishu:oc_target",
    )

    target = loop.sessions.get_or_create("feishu:oc_target")
    assert target.key == "feishu:oc_target"
    assert target.messages == source.messages
    assert target.metadata == source.metadata

    # verify deep copy (mutating source after fork does not change target)
    source.metadata["x"].append(4)
    source.messages.append({"role": "assistant", "content": "new", "timestamp": "2026-03-23T10:00:02"})
    assert target.metadata["x"] == [1, 2, 3]
    assert len(target.messages) == 2
