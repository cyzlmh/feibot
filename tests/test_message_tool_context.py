import asyncio

import pytest

from feibot.agent.tools.message import MessageTool
from feibot.bus.events import OutboundMessage


@pytest.mark.asyncio
async def test_message_tool_context_is_task_local() -> None:
    sent: list[tuple[str, str, str]] = []

    async def _send(msg: OutboundMessage) -> None:
        await asyncio.sleep(0)
        sent.append((msg.channel, msg.chat_id, msg.content))

    tool = MessageTool(send_callback=_send)

    async def _worker(channel: str, chat_id: str, content: str) -> bool:
        tool.set_context(channel, chat_id)
        tool.start_turn()
        await asyncio.sleep(0)
        await tool.execute(content)
        return tool._sent_in_turn

    sent_flags = await asyncio.gather(
        _worker("feishu", "oc_1", "one"),
        _worker("cli", "cli_2", "two"),
    )

    assert all(sent_flags)
    assert sorted(sent) == [
        ("cli", "cli_2", "two"),
        ("feishu", "oc_1", "one"),
    ]


@pytest.mark.asyncio
async def test_message_tool_finish_flag_is_task_local() -> None:
    sent: list[tuple[str, str, str]] = []

    async def _send(msg: OutboundMessage) -> None:
        await asyncio.sleep(0)
        sent.append((msg.channel, msg.chat_id, msg.content))

    tool = MessageTool(send_callback=_send)

    async def _worker(channel: str, chat_id: str, content: str, finish: bool) -> bool:
        tool.set_context(channel, chat_id)
        tool.start_turn()
        await asyncio.sleep(0)
        await tool.execute(content, finish=finish)
        return tool.finish_requested

    finish_flags = await asyncio.gather(
        _worker("feishu", "oc_1", "done-1", True),
        _worker("cli", "cli_2", "done-2", False),
    )

    assert sorted(finish_flags) == [False, True]
    assert sorted(sent) == [
        ("cli", "cli_2", "done-2"),
        ("feishu", "oc_1", "done-1"),
    ]
