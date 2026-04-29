import asyncio
from pathlib import Path

from feibot.agent.loop import AgentLoop
from feibot.bus.events import InboundMessage
from feibot.bus.queue import MessageBus
from feibot.config.schema import MadameConfig
from feibot.providers.base import LLMProvider, LLMResponse


class _DummyProvider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        return LLMResponse(content="unused")

    def get_default_model(self) -> str:
        return "dummy/test-model"


def test_agent_command_rejected_when_madame_disabled(tmp_path: Path) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_DummyProvider(),
        workspace=tmp_path,
        model="dummy/test-model",
    )

    response = asyncio.run(
        loop._process_message(
            InboundMessage(
                channel="feishu",
                sender_id="ou_test",
                chat_id="oc_mgr_1",
                content="/agent list",
                metadata={"msg_type": "text", "message_id": "om_agent_list"},
            )
        )
    )

    assert response is not None
    assert "Madame commands are disabled" in response.content


def test_agent_command_delegates_to_madame_controller(tmp_path: Path) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_DummyProvider(),
        workspace=tmp_path,
        model="dummy/test-model",
        madame_config=MadameConfig(enabled=True),
    )

    calls: list[str] = []

    class _FakeMadame:
        def execute(self, args: str) -> str:
            calls.append(args)
            return "madame-ok"

    loop.madame_controller = _FakeMadame()

    response = asyncio.run(
        loop._process_message(
            InboundMessage(
                channel="feishu",
                sender_id="ou_test",
                chat_id="oc_mgr_2",
                content="@_user_1 /agent list",
                metadata={"msg_type": "text", "message_id": "om_agent_list2"},
            )
        )
    )

    assert response is not None
    assert response.content == "madame-ok"
    assert calls == ["list"]
