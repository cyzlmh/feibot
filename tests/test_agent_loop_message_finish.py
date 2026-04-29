import asyncio
from pathlib import Path

import pytest

from feibot.agent.loop import AgentLoop
from feibot.agent.tools.message import MessageTool
from feibot.bus.events import InboundMessage
from feibot.bus.queue import MessageBus
from feibot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class FinishMessageProvider(LLMProvider):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call_finish_message",
                        name="message",
                        arguments={"content": "done", "finish": True},
                    )
                ],
            )
        raise AssertionError("Provider should not be called again after message(finish=true)")

    def get_default_model(self) -> str:
        return "dummy/test-model"


class MessageOnlyAgentLoop(AgentLoop):
    def _register_default_tools(self) -> None:  # pragma: no cover - test helper
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))


def _make_loop(tmp_path: Path, bus: MessageBus, provider: LLMProvider) -> AgentLoop:
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    return MessageOnlyAgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        model="dummy/test-model",
        memory_window=20,
        max_iterations=20,
    )


@pytest.mark.asyncio
async def test_message_finish_stops_agent_loop_early(tmp_path: Path) -> None:
    bus = MessageBus()
    provider = FinishMessageProvider()
    loop = _make_loop(tmp_path, bus=bus, provider=provider)

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_test",
            chat_id="ou_test",
            content="ping",
            metadata={
                "msg_type": "text",
                "message_id": "om_test_finish",
                "_suppress_progress": True,
            },
        )
    )

    assert response is None
    assert provider.calls == 1

    outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    assert outbound.channel == "feishu"
    assert outbound.chat_id == "ou_test"
    assert outbound.content == "done"

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.consume_outbound(), timeout=0.05)
