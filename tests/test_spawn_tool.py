import asyncio

import pytest

from feibot.agent.loop import AgentLoop
from feibot.agent.subagent import SubagentManager
from feibot.agent.tools.spawn import SpawnTool
from feibot.bus.events import InboundMessage
from feibot.bus.queue import MessageBus
from feibot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


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


class _SpawnFinishProvider(LLMProvider):
    def __init__(self):
        super().__init__()
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
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call_spawn_finish",
                        name="spawn",
                        arguments={"task": "Handle a long task", "label": "long-task"},
                    )
                ],
            )
        raise AssertionError("Provider should not be called again after successful spawn")

    def get_default_model(self) -> str:
        return "fake-model"


@pytest.mark.asyncio
async def test_spawn_tool_passes_context_to_manager():
    class _Manager:
        def __init__(self):
            self.kwargs = None

        async def spawn(self, **kwargs):
            self.kwargs = kwargs
            return "ok"

    mgr = _Manager()
    tool = SpawnTool(manager=mgr)
    tool.set_context("cli", "direct")

    result = await tool.execute(task="Index repo", label="index")

    assert result == "ok"
    assert mgr.kwargs is not None
    assert mgr.kwargs["task"] == "Index repo"
    assert mgr.kwargs["label"] == "index"
    assert mgr.kwargs["origin_channel"] == "cli"
    assert mgr.kwargs["origin_chat_id"] == "direct"


@pytest.mark.asyncio
async def test_spawn_tool_uses_feishu_visible_chat_path(monkeypatch):
    class _Manager:
        def __init__(self):
            self.spawn_called = False

        async def spawn(self, **kwargs):
            self.spawn_called = True
            return "hidden"

    mgr = _Manager()
    tool = SpawnTool(
        manager=mgr,
        feishu_app_id="app",
        feishu_app_secret="secret",
        feishu_default_member_open_id="ou_owner",
    )
    tool.set_context("feishu", "oc_parent_chat", "ou_user_123")

    seen = {}

    async def _fake_feishu_spawn(task: str, label: str | None = None):
        seen["task"] = task
        seen["label"] = label
        return "visible"

    monkeypatch.setattr(tool, "_spawn_feishu_task_chat", _fake_feishu_spawn)

    result = await tool.execute(task="Research logs", label="triage")

    assert result == "visible"
    assert mgr.spawn_called is False
    assert seen == {"task": "Research logs", "label": "triage"}


def test_spawn_tool_description_mentions_only_direct_policy_for_ou_chat():
    tool = SpawnTool(manager=object())  # type: ignore[arg-type]
    tool.set_context("feishu", "ou_123")
    desc = tool.description
    assert "Feishu direct chat (`ou_*`)" in desc
    assert "Feishu group chat (`oc_*`)" not in desc


def test_spawn_tool_description_mentions_only_group_policy_for_oc_chat():
    tool = SpawnTool(manager=object())  # type: ignore[arg-type]
    tool.set_context("feishu", "oc_123")
    desc = tool.description
    assert "Feishu group chat (`oc_*`)" in desc
    assert "Feishu direct chat (`ou_*`)" not in desc


@pytest.mark.asyncio
async def test_spawn_bootstrap_marks_session_as_subagent_and_hides_spawn_tool(monkeypatch, tmp_path):
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_FakeProvider(LLMResponse(content="unused")),
        workspace=tmp_path,
        model="fake-model",
    )
    seen: dict[str, object] = {}

    async def _fake_run_agent_loop(
        initial_messages,
        user_goal,
        debug_log=None,
        on_progress=None,
        disabled_tools=None,
        on_checkpoint=None,
    ):
        seen["disabled_tools"] = disabled_tools
        return "done", [], {"history_messages": [], "stopped_reason": "stop"}

    monkeypatch.setattr(loop, "_run_agent_loop", _fake_run_agent_loop)

    resp = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_owner",
            chat_id="oc_child",
            content="请继续执行任务",
            metadata={
                "msg_type": "text",
                "message_id": "om_bootstrap_1",
                "_spawn_bootstrap": True,
            },
        )
    )

    assert resp is not None
    assert resp.content == "done"
    session = loop.sessions.get_or_create("feishu:oc_child")
    assert session.metadata.get(loop.SPAWN_CHILD_SESSION_METADATA_KEY) is True
    assert seen.get("disabled_tools") == {"spawn"}


@pytest.mark.asyncio
async def test_subagent_manager_announces_result_to_bus(tmp_path):
    provider = _FakeProvider(LLMResponse(content="Background check complete."))
    bus = MessageBus()
    mgr = SubagentManager(provider=provider, workspace=tmp_path, bus=bus)

    ack = await mgr.spawn(
        task="Check the workspace and report.",
        label="workspace-check",
        origin_channel="feishu",
        origin_chat_id="oc_chat_456",
    )
    assert "started" in ack.lower()

    running = list(mgr._running_tasks.values())
    assert len(running) == 1
    await asyncio.wait_for(running[0], timeout=2.0)

    msg = await asyncio.wait_for(bus.consume_inbound(), timeout=2.0)
    assert msg.channel == "system"
    assert msg.chat_id == "feishu:oc_chat_456"
    assert msg.sender_id == "subagent"
    assert "workspace-check" in msg.content
    assert "Background check complete." in msg.content
    assert msg.metadata.get("_suppress_progress") is True


@pytest.mark.asyncio
async def test_sp_command_opens_feishu_session(monkeypatch, tmp_path):
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_FakeProvider(LLMResponse(content="unused")),
        workspace=tmp_path,
        model="fake-model",
    )
    spawn_tool = loop.tools.get("spawn")
    assert isinstance(spawn_tool, SpawnTool)

    seen: dict[str, str | None] = {}

    async def _fake_open_session(label: str | None = None):
        seen["label"] = label
        return "opened"

    monkeypatch.setattr(spawn_tool, "open_session", _fake_open_session)

    resp = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_test",
            chat_id="oc_parent_1",
            content="@_user_1 /sp quick triage",
            metadata={"msg_type": "text", "message_id": "om_sp_1"},
        )
    )

    assert resp is not None
    assert resp.content == "opened"
    assert seen == {"label": "quick triage"}


@pytest.mark.asyncio
async def test_successful_spawn_stops_agent_loop_early(monkeypatch, tmp_path):
    provider = _SpawnFinishProvider()
    bus = MessageBus()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="fake-model",
    )
    spawn_result = (
        "Created Feishu subtask chat `feibot-subtask-1234` (oc_test). "
        "Continue and review the full task trajectory there."
    )

    async def _fake_execute(name: str, arguments):
        assert name == "spawn"
        return spawn_result

    monkeypatch.setattr(loop.tools, "execute", _fake_execute)

    resp = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_test",
            chat_id="ou_test",
            content="start a long task",
            metadata={
                "msg_type": "text",
                "message_id": "om_spawn_finish",
                "_suppress_progress": True,
            },
        )
    )

    assert resp is not None
    assert resp.content == spawn_result
    assert provider.calls == 1
