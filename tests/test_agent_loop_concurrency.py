import asyncio
import time
from pathlib import Path

import pytest

from feibot.agent.loop import AgentLoop
from feibot.bus.events import InboundMessage
from feibot.bus.queue import MessageBus
from feibot.providers.base import LLMProvider, LLMResponse


class SleepProvider(LLMProvider):
    def __init__(self, delay_s: float = 0.25):
        super().__init__()
        self.delay_s = delay_s

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        await asyncio.sleep(self.delay_s)
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy/test-model"


class NoToolsAgentLoop(AgentLoop):
    def _register_default_tools(self) -> None:  # pragma: no cover - test helper
        return None


def _make_loop(tmp_path: Path, bus: MessageBus, provider: LLMProvider) -> AgentLoop:
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    return NoToolsAgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        model="dummy/test-model",
        memory_window=20,
    )


@pytest.mark.asyncio
async def test_run_processes_different_sessions_in_parallel(tmp_path: Path) -> None:
    bus = MessageBus()
    loop = _make_loop(tmp_path, bus=bus, provider=SleepProvider(0.25))

    runner = asyncio.create_task(loop.run())
    try:
        start = time.perf_counter()
        await bus.publish_inbound(
            InboundMessage(channel="feishu", sender_id="ou_1", chat_id="oc_a", content="msg-a")
        )
        await bus.publish_inbound(
            InboundMessage(channel="feishu", sender_id="ou_2", chat_id="oc_b", content="msg-b")
        )
        out1 = await asyncio.wait_for(bus.consume_outbound(), timeout=2.0)
        out2 = await asyncio.wait_for(bus.consume_outbound(), timeout=2.0)
        elapsed = time.perf_counter() - start

        assert {out1.chat_id, out2.chat_id} == {"oc_a", "oc_b"}
        assert elapsed < 0.45
    finally:
        loop.stop()
        await asyncio.wait_for(runner, timeout=2.0)


@pytest.mark.asyncio
async def test_run_keeps_same_session_serialized(tmp_path: Path) -> None:
    bus = MessageBus()
    loop = _make_loop(tmp_path, bus=bus, provider=SleepProvider(0.25))

    runner = asyncio.create_task(loop.run())
    try:
        start = time.perf_counter()
        await bus.publish_inbound(
            InboundMessage(channel="feishu", sender_id="ou_1", chat_id="oc_same", content="msg-1")
        )
        await bus.publish_inbound(
            InboundMessage(channel="feishu", sender_id="ou_1", chat_id="oc_same", content="msg-2")
        )
        await asyncio.wait_for(bus.consume_outbound(), timeout=2.0)
        await asyncio.wait_for(bus.consume_outbound(), timeout=2.0)
        elapsed = time.perf_counter() - start

        assert elapsed >= 0.45
    finally:
        loop.stop()
        await asyncio.wait_for(runner, timeout=2.0)
