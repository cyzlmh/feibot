import asyncio
import re
from pathlib import Path

import pytest

from feibot.agent.loop import AgentLoop
from feibot.bus.events import InboundMessage
from feibot.bus.queue import MessageBus
from feibot.config.schema import ExecToolConfig
from feibot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class ExecApprovalProvider(LLMProvider):
    def __init__(self, command: str):
        super().__init__()
        self.command = command
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call_exec_1",
                        name="exec",
                        arguments={"command": self.command},
                    )
                ],
            )
        if self.calls == 2:
            return LLMResponse(content="done")
        raise AssertionError("Unexpected extra provider call")

    def get_default_model(self) -> str:
        return "dummy/test-model"


class ReplaySensitiveProvider(LLMProvider):
    """Second turn retries exec only when deny history is missing."""

    def __init__(self, command: str):
        super().__init__()
        self.command = command
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call_exec_replay_1",
                        name="exec",
                        arguments={"command": self.command},
                    )
                ],
            )
        if self.calls == 2:
            history_blob = "\n".join(str(m.get("content") or "") for m in messages if isinstance(m, dict))
            if "exec approval denied" in history_blob:
                return LLMResponse(content="got it, approval was denied. no retry.")
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call_exec_replay_2",
                        name="exec",
                        arguments={"command": self.command},
                    )
                ],
            )
        return LLMResponse(content="done")

    def get_default_model(self) -> str:
        return "dummy/test-model"


class DenyThenContinueProvider(LLMProvider):
    def __init__(self, command: str):
        super().__init__()
        self.command = command
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call_exec_deny_1",
                        name="exec",
                        arguments={"command": self.command},
                    )
                ],
            )
        return LLMResponse(content="fresh turn works")

    def get_default_model(self) -> str:
        return "dummy/test-model"


@pytest.mark.asyncio
async def test_exec_approval_pending_then_approve_resumes_blocked_loop(tmp_path: Path) -> None:
    bus = MessageBus()
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)

    target = workspace / "cleanup.txt"
    target.write_text("trash", encoding="utf-8")
    command = f"rm -f {target}"

    provider = ExecApprovalProvider(command)
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        model="dummy/test-model",
        max_iterations=5,
        memory_window=20,
        restrict_to_workspace=True,
        exec_config=ExecToolConfig(approval_risk_level="confirm"),
    )

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_requester",
            chat_id="oc_group_1",
            content="Please clean temp file",
            metadata={
                "msg_type": "text",
                "message_id": "om_exec_1",
                "_suppress_progress": True,
            },
        )
    )

    assert response is None
    card_prompt = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    assert card_prompt.metadata is not None
    approval_id = str(card_prompt.metadata.get("_exec_approval_id") or "")
    assert re.fullmatch(r"[0-9a-f]{10}", approval_id)
    assert card_prompt.metadata.get("_exec_approval_id") == approval_id
    card = card_prompt.metadata.get("_feishu_card")
    assert isinstance(card, dict)
    assert card.get("schema") == "2.0"
    body = card.get("body", {})
    elements = body.get("elements", [])
    assert isinstance(elements, list)
    assert "About to run this shell command" in str(elements[0].get("content", ""))
    column_set = elements[-1]
    columns = column_set.get("columns", [])
    labels = []
    callback_values = []
    for col in columns:
        col_elements = col.get("elements") if isinstance(col, dict) else None
        first = col_elements[0] if isinstance(col_elements, list) and col_elements else {}
        text = first.get("text") if isinstance(first, dict) else {}
        labels.append(text.get("content") if isinstance(text, dict) else None)
        behaviors = first.get("behaviors") if isinstance(first, dict) else None
        callback = behaviors[0] if isinstance(behaviors, list) and behaviors else {}
        callback_values.append(callback.get("value") if isinstance(callback, dict) else None)
    assert labels == ["Allow Once", "Deny"]
    for item in callback_values:
        assert isinstance(item, dict)
        assert item.get("approval_id") == approval_id
        assert item.get("risk_level") == "confirm"
        assert item.get("working_dir") == str(workspace)
        assert item.get("command_preview")

    approve_response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_requester",
            chat_id="oc_group_1",
            content=f"/approve {approval_id} allow-once",
            metadata={
                "msg_type": "interactive",
                "message_id": "om_exec_2",
                "_suppress_progress": True,
                "source": "card_action",
            },
        )
    )

    assert approve_response is None

    final_notice = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    assert final_notice.content == "done"
    assert not target.exists()
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_exec_approval_can_resume_after_gateway_restart(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)

    target = workspace / "cleanup_restart.txt"
    target.write_text("trash", encoding="utf-8")
    command = f"rm -f {target}"

    provider = ExecApprovalProvider(command)
    first_bus = MessageBus()
    first_loop = AgentLoop(
        bus=first_bus,
        provider=provider,
        workspace=workspace,
        model="dummy/test-model",
        max_iterations=5,
        memory_window=20,
        restrict_to_workspace=True,
        exec_config=ExecToolConfig(approval_risk_level="confirm"),
    )

    first_response = await first_loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_requester",
            chat_id="oc_group_1",
            content="Please clean temp file",
            metadata={
                "msg_type": "text",
                "message_id": "om_exec_restart_1",
                "_suppress_progress": True,
            },
        )
    )
    assert first_response is None

    card_prompt = await asyncio.wait_for(first_bus.consume_outbound(), timeout=1.0)
    approval_id = str(card_prompt.metadata.get("_exec_approval_id") or "")
    assert re.fullmatch(r"[0-9a-f]{10}", approval_id)

    second_bus = MessageBus()
    second_loop = AgentLoop(
        bus=second_bus,
        provider=provider,
        workspace=workspace,
        model="dummy/test-model",
        max_iterations=5,
        memory_window=20,
        restrict_to_workspace=True,
        exec_config=ExecToolConfig(approval_risk_level="confirm"),
    )

    approve_response = await second_loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_requester",
            chat_id="oc_group_1",
            content=f"/approve {approval_id} allow-once",
            metadata={
                "msg_type": "interactive",
                "message_id": "om_exec_restart_2",
                "_suppress_progress": True,
                "source": "card_action",
            },
        )
    )

    assert approve_response is None

    final_notice = await asyncio.wait_for(second_bus.consume_outbound(), timeout=1.0)
    assert final_notice.content == "done"
    assert not target.exists()
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_exec_approval_rejects_manual_text_approve_for_card_flow(tmp_path: Path) -> None:
    bus = MessageBus()
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)

    target = workspace / "cleanup_manual_text.txt"
    target.write_text("trash", encoding="utf-8")
    command = f"rm -f {target}"

    provider = ExecApprovalProvider(command)
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        model="dummy/test-model",
        max_iterations=5,
        memory_window=20,
        restrict_to_workspace=True,
        exec_config=ExecToolConfig(approval_risk_level="confirm"),
    )

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_requester",
            chat_id="oc_group_1",
            content="Please clean temp file",
            metadata={
                "msg_type": "text",
                "message_id": "om_exec_text_1",
                "_suppress_progress": True,
            },
        )
    )

    assert response is None
    card_prompt = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    approval_id = str(card_prompt.metadata.get("_exec_approval_id") or "")

    approve_response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_requester",
            chat_id="oc_group_1",
            content=f"/approve {approval_id} allow-once",
            metadata={
                "msg_type": "text",
                "message_id": "om_exec_text_2",
                "_suppress_progress": True,
            },
        )
    )

    assert approve_response is not None
    assert "Text approval is disabled" in approve_response.content
    assert target.exists()
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_exec_confirm_risk_runs_without_hitl_when_threshold_is_dangerous(tmp_path: Path) -> None:
    bus = MessageBus()
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)

    target = workspace / "cleanup_none_mode.txt"
    target.write_text("trash", encoding="utf-8")
    command = f"rm -f {target}"

    provider = ExecApprovalProvider(command)
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        model="dummy/test-model",
        max_iterations=5,
        memory_window=20,
        restrict_to_workspace=True,
        exec_config=ExecToolConfig(approval_risk_level="dangerous"),
    )

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_requester",
            chat_id="oc_group_1",
            content="Please clean temp file",
            metadata={
                "msg_type": "text",
                "message_id": "om_exec_none_1",
                "_suppress_progress": True,
            },
        )
    )

    assert response is not None
    assert response.content == "done"
    assert not target.exists()
    assert provider.calls == 2
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.consume_outbound(), timeout=0.1)


@pytest.mark.asyncio
async def test_exec_approval_card_deny_stops_loop(tmp_path: Path) -> None:
    bus = MessageBus()
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)

    target = workspace / "cleanup_card_deny.txt"
    target.write_text("trash", encoding="utf-8")
    command = f"rm -f {target}"

    provider = ExecApprovalProvider(command)
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        model="dummy/test-model",
        max_iterations=5,
        memory_window=20,
        restrict_to_workspace=True,
        exec_config=ExecToolConfig(approval_risk_level="confirm"),
    )

    first = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_requester",
            chat_id="oc_group_1",
            content="Please clean temp file",
            metadata={
                "msg_type": "text",
                "message_id": "om_exec_card_deny_1",
                "_suppress_progress": True,
            },
        )
    )
    assert first is None

    card_prompt = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    approval_id = str(card_prompt.metadata.get("_exec_approval_id") or "")

    deny_response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_requester",
            chat_id="oc_group_1",
            content=f"/approve {approval_id} deny",
            metadata={
                "msg_type": "interactive",
                "message_id": "om_exec_card_deny_2",
                "_suppress_progress": True,
                "source": "card_action",
            },
        )
    )

    assert deny_response is not None
    assert deny_response.content == f"✅ Exec approval denied (ID: {approval_id})."
    assert target.exists()
    assert provider.calls == 1

    session = loop.sessions.get_or_create("feishu:oc_group_1")
    assert AgentLoop.RESUME_STATE_METADATA_KEY not in session.metadata
    history = session.get_history(max_messages=50)
    assert any(
        m.get("role") == "tool" and "exec approval denied" in str(m.get("content", ""))
        for m in history
    )
    assert any(
        m.get("role") == "assistant" and "Exec approval denied" in str(m.get("content", ""))
        for m in history
    )


def test_unset_approval_risk_level_defaults_to_none(tmp_path: Path) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=ExecApprovalProvider("rm -f /tmp/unused"),
        workspace=tmp_path,
        model="dummy/test-model",
        memory_window=20,
        exec_config=ExecToolConfig(),
    )

    assert loop._approval_workflow("feishu", risk_level="confirm") == "none"
    assert loop._approval_workflow("feishu", risk_level="dangerous") == "none"


@pytest.mark.asyncio
async def test_exec_approval_card_deny_prevents_next_turn_replay(tmp_path: Path) -> None:
    bus = MessageBus()
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)

    target = workspace / "cleanup_card_replay.txt"
    target.write_text("trash", encoding="utf-8")
    command = f"rm -f {target}"

    provider = ReplaySensitiveProvider(command)
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        model="dummy/test-model",
        max_iterations=5,
        memory_window=20,
        restrict_to_workspace=True,
        exec_config=ExecToolConfig(approval_risk_level="confirm"),
    )

    first = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_requester",
            chat_id="oc_group_1",
            content="delete the file",
            metadata={
                "msg_type": "text",
                "message_id": "om_exec_card_replay_1",
                "_suppress_progress": True,
            },
        )
    )
    assert first is None

    card_prompt = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    approval_id = str(card_prompt.metadata.get("_exec_approval_id") or "")
    deny_response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_requester",
            chat_id="oc_group_1",
            content=f"/approve {approval_id} deny",
            metadata={
                "msg_type": "interactive",
                "message_id": "om_exec_card_replay_2",
                "_suppress_progress": True,
                "source": "card_action",
            },
        )
    )
    assert deny_response is not None
    assert deny_response.content == f"✅ Exec approval denied (ID: {approval_id})."

    second = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_requester",
            chat_id="oc_group_1",
            content="now just say hi",
            metadata={
                "msg_type": "text",
                "message_id": "om_exec_card_replay_3",
                "_suppress_progress": True,
            },
        )
    )
    assert second is not None
    assert "no retry" in second.content
    assert provider.calls == 2
    assert target.exists()


def test_confirm_threshold_covers_dangerous_commands(tmp_path: Path) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=ExecApprovalProvider("rm -f /tmp/unused"),
        workspace=tmp_path,
        model="dummy/test-model",
        memory_window=20,
        exec_config=ExecToolConfig(approval_risk_level="confirm"),
    )

    assert (
        loop._approval_workflow("feishu", risk_level="dangerous")
        == "feishu_card"
    )


@pytest.mark.asyncio
async def test_exec_approval_dangerous_threshold_uses_feishu_card(tmp_path: Path) -> None:
    bus = MessageBus()
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)

    command = "rm -rf /"
    provider = ExecApprovalProvider(command)
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        model="dummy/test-model",
        max_iterations=5,
        memory_window=20,
        restrict_to_workspace=False,
        exec_config=ExecToolConfig(approval_risk_level="dangerous"),
    )

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_requester",
            chat_id="oc_group_1",
            content="run dangerous cleanup",
            metadata={
                "msg_type": "text",
                "message_id": "om_exec_card_hd_1",
                "_suppress_progress": True,
            },
        )
    )

    assert response is None
    card_prompt = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    approval_id = str(card_prompt.metadata.get("_exec_approval_id") or "")
    assert approval_id
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_exec_approval_card_deny_in_run_loop_does_not_hang_and_allows_new_turn(
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)

    target = workspace / "cleanup_run_deny.txt"
    target.write_text("trash", encoding="utf-8")
    command = f"rm -f {target}"

    provider = DenyThenContinueProvider(command)
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        model="dummy/test-model",
        max_iterations=5,
        memory_window=20,
        restrict_to_workspace=True,
        exec_config=ExecToolConfig(approval_risk_level="confirm"),
    )

    runner = asyncio.create_task(loop.run())
    try:
        await bus.publish_inbound(
            InboundMessage(
                channel="feishu",
                sender_id="ou_requester",
                chat_id="oc_group_1",
                content="delete temp file",
                metadata={
                    "msg_type": "text",
                    "message_id": "om_exec_run_deny_1",
                    "_suppress_progress": True,
                },
            )
        )

        card_prompt = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        approval_id = str(card_prompt.metadata.get("_exec_approval_id") or "")
        assert approval_id

        await bus.publish_inbound(
            InboundMessage(
                channel="feishu",
                sender_id="ou_requester",
                chat_id="oc_group_1",
                content=f"/approve {approval_id} deny",
                metadata={
                    "msg_type": "interactive",
                    "message_id": "om_exec_run_deny_2",
                    "_suppress_progress": True,
                    "source": "card_action",
                },
            )
        )
        deny_reply = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        assert deny_reply.content == f"✅ Exec approval denied (ID: {approval_id})."

        session = loop.sessions.get_or_create("feishu:oc_group_1")
        assert AgentLoop.RESUME_STATE_METADATA_KEY not in session.metadata
        assert target.exists()

        await bus.publish_inbound(
            InboundMessage(
                channel="feishu",
                sender_id="ou_requester",
                chat_id="oc_group_1",
                content="say hello",
                metadata={
                    "msg_type": "text",
                    "message_id": "om_exec_run_deny_3",
                    "_suppress_progress": True,
                },
            )
        )
        follow_up = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        assert follow_up.content == "fresh turn works"
        assert provider.calls == 2
    finally:
        loop.stop()
        await asyncio.wait_for(runner, timeout=2.0)
