import asyncio
from pathlib import Path

import pytest

from feibot.agent.loop import AgentLoop
from feibot.bus.events import InboundMessage, OutboundMessage
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


def _make_loop(tmp_path: Path, bus: MessageBus | None = None) -> AgentLoop:
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    return NoToolsAgentLoop(
        bus=bus or MessageBus(),
        provider=DummyProvider(),
        workspace=workspace,
        model="dummy/test-model",
        memory_window=20,
    )


@pytest.mark.asyncio
async def test_stop_command_cancels_active_session_task(monkeypatch, tmp_path: Path) -> None:
    bus = MessageBus()
    loop = _make_loop(tmp_path, bus=bus)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _fake_process_message(msg: InboundMessage, session_key: str | None = None):
        if msg.content != "long task":
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="ok")
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="done")

    monkeypatch.setattr(loop, "_process_message", _fake_process_message)

    runner = asyncio.create_task(loop.run())
    try:
        await bus.publish_inbound(
            InboundMessage(
                channel="feishu",
                sender_id="ou_test",
                chat_id="oc_group_1",
                content="long task",
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)

        await bus.publish_inbound(
            InboundMessage(
                channel="feishu",
                sender_id="ou_test",
                chat_id="oc_group_1",
                content="/stop",
            )
        )

        stop_reply = await asyncio.wait_for(bus.consume_outbound(), timeout=2.0)
        assert "Stopped" in stop_reply.content
        await asyncio.wait_for(cancelled.wait(), timeout=2.0)
    finally:
        loop.stop()
        await asyncio.wait_for(runner, timeout=2.0)


@pytest.mark.asyncio
async def test_stop_command_cancels_waiting_session_tasks_and_releases_lock(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    loop = _make_loop(tmp_path, bus=bus)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _fake_process_message(msg: InboundMessage, session_key: str | None = None):
        if msg.content == "long task":
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="done")
        if msg.content == "queued task":
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="queued")
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="fresh")

    monkeypatch.setattr(loop, "_process_message", _fake_process_message)

    runner = asyncio.create_task(loop.run())
    try:
        await bus.publish_inbound(
            InboundMessage(
                channel="feishu",
                sender_id="ou_test",
                chat_id="oc_group_lock",
                content="long task",
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)

        await bus.publish_inbound(
            InboundMessage(
                channel="feishu",
                sender_id="ou_test",
                chat_id="oc_group_lock",
                content="queued task",
            )
        )
        await bus.publish_inbound(
            InboundMessage(
                channel="feishu",
                sender_id="ou_test",
                chat_id="oc_group_lock",
                content="/stop",
            )
        )

        stop_reply = await asyncio.wait_for(bus.consume_outbound(), timeout=2.0)
        assert "Stopped" in stop_reply.content
        await asyncio.wait_for(cancelled.wait(), timeout=2.0)

        await bus.publish_inbound(
            InboundMessage(
                channel="feishu",
                sender_id="ou_test",
                chat_id="oc_group_lock",
                content="after stop",
            )
        )
        post_stop_reply = await asyncio.wait_for(bus.consume_outbound(), timeout=2.0)
        assert post_stop_reply.content == "fresh"

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bus.consume_outbound(), timeout=0.2)
    finally:
        loop.stop()
        await asyncio.wait_for(runner, timeout=2.0)
