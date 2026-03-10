import asyncio
import re
from pathlib import Path

import pytest

from feibot.agent.loop import AgentLoop
from feibot.agent.sim_auth import SimAuthDecision
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
            if "SimAuth denied exec approval" in history_blob or "exec approval denied" in history_blob:
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
        exec_config=ExecToolConfig(approval_confirm_mode="feishu_card"),
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
        exec_config=ExecToolConfig(approval_confirm_mode="feishu_card"),
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
async def test_exec_confirm_mode_none_runs_without_hitl(tmp_path: Path) -> None:
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
        exec_config=ExecToolConfig(
            approval_confirm_mode="none",
            approval_dangerous_mode="feishu_card",
        ),
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
async def test_exec_approval_sim_auth_auto_allow_resumes_loop(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)

    target = workspace / "cleanup_sim_allow.txt"
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
        exec_config=ExecToolConfig(
            approval_confirm_mode="sim_auth",
            approval_sim_auth_url="https://sim-auth.local/verify",
        ),
    )

    async def _fake_decision(request):  # noqa: ANN001
        assert request.command == command
        return "allow-once", "approved by SIM auth"

    monkeypatch.setattr(loop, "_request_sim_auth_decision", _fake_decision)

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_requester",
            chat_id="oc_group_1",
            content="Please clean temp file",
            metadata={
                "msg_type": "text",
                "message_id": "om_exec_sim_1",
                "_suppress_progress": True,
            },
        )
    )

    assert response is not None
    assert "SimAuth verification" in response.content

    final_notice = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    assert final_notice.content == "done"
    assert not target.exists()
    assert provider.calls == 2


def test_sim_auth_mode_is_unavailable_without_requester_support(tmp_path: Path) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=ExecApprovalProvider("rm -f /tmp/unused"),
        workspace=tmp_path,
        model="dummy/test-model",
        memory_window=20,
        exec_config=ExecToolConfig(
            approval_confirm_mode="sim_auth",
            approval_sim_auth_host="https://ptest.cmccsim.com:9090",
            approval_sim_auth_send_auth_path="/trustedAuth/api/simAuth/sendAuth",
            approval_sim_auth_get_result_path="/trustedAuth/api/simAuth/getSimAuthResult",
            approval_sim_auth_ap_id="A0003",
            approval_sim_auth_app_id="A0003001",
            approval_sim_auth_private_key="test-private-key",
            approval_sim_auth_template_id="DF20240419093514451c32",
        ),
    )

    assert loop._approval_mode("feishu", sender_id="ou_requester", risk_level="confirm") == "unavailable"


def test_unset_approval_modes_default_to_none(tmp_path: Path) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=ExecApprovalProvider("rm -f /tmp/unused"),
        workspace=tmp_path,
        model="dummy/test-model",
        memory_window=20,
        exec_config=ExecToolConfig(),
    )

    assert loop._approval_mode("feishu", sender_id="ou_requester", risk_level="confirm") == "none"
    assert loop._approval_mode("feishu", sender_id="ou_requester", risk_level="dangerous") == "none"


@pytest.mark.asyncio
async def test_exec_approval_sim_auth_auto_deny_stops_loop(monkeypatch, tmp_path: Path) -> None:
    bus = MessageBus()
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)

    target = workspace / "cleanup_sim_deny.txt"
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
        exec_config=ExecToolConfig(
            approval_confirm_mode="sim_auth",
            approval_sim_auth_url="https://sim-auth.local/verify",
        ),
    )

    async def _fake_decision(_request):  # noqa: ANN001
        return "deny", "user rejected on SIM channel"

    monkeypatch.setattr(loop, "_request_sim_auth_decision", _fake_decision)

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_requester",
            chat_id="oc_group_1",
            content="Please clean temp file",
            metadata={
                "msg_type": "text",
                "message_id": "om_exec_sim_2",
                "_suppress_progress": True,
            },
        )
    )

    assert response is not None
    assert "SimAuth verification" in response.content

    deny_notice = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    assert "SimAuth denied exec approval" in deny_notice.content
    assert "user rejected on SIM channel" in deny_notice.content
    assert target.exists()
    assert provider.calls == 1

    session = loop.sessions.get_or_create("feishu:oc_group_1")
    history = session.get_history(max_messages=50)
    assert any(
        m.get("role") == "tool" and "exec approval denied" in str(m.get("content", ""))
        for m in history
    )
    assert any(
        m.get("role") == "assistant" and "SimAuth denied exec approval" in str(m.get("content", ""))
        for m in history
    )


@pytest.mark.asyncio
async def test_exec_approval_sim_auth_deny_reason_masks_success_word(monkeypatch, tmp_path: Path) -> None:
    bus = MessageBus()
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)

    target = workspace / "cleanup_sim_deny_reason.txt"
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
        exec_config=ExecToolConfig(
            approval_confirm_mode="sim_auth",
            approval_sim_auth_url="https://sim-auth.local/verify",
        ),
    )

    async def _fake_verify(_request):  # noqa: ANN001
        return SimAuthDecision(decision="deny", reason="成功")

    monkeypatch.setattr(loop.sim_auth_resolver, "verify", _fake_verify)

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_requester",
            chat_id="oc_group_1",
            content="Please clean temp file",
            metadata={
                "msg_type": "text",
                "message_id": "om_exec_sim_3",
                "_suppress_progress": True,
            },
        )
    )

    assert response is not None
    deny_notice = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    assert "SimAuth denied exec approval" in deny_notice.content
    assert "Reason: 成功" not in deny_notice.content
    assert "Reason: SIM auth rejected." in deny_notice.content


@pytest.mark.asyncio
async def test_exec_approval_sim_auth_deny_prevents_next_turn_replay(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)

    target = workspace / "cleanup_sim_replay.txt"
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
        exec_config=ExecToolConfig(
            approval_confirm_mode="sim_auth",
            approval_sim_auth_url="https://sim-auth.local/verify",
        ),
    )

    async def _fake_decision(_request):  # noqa: ANN001
        return "deny", "status=1"

    monkeypatch.setattr(loop, "_request_sim_auth_decision", _fake_decision)

    first = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_requester",
            chat_id="oc_group_1",
            content="delete the file",
            metadata={
                "msg_type": "text",
                "message_id": "om_exec_sim_replay_1",
                "_suppress_progress": True,
            },
        )
    )
    assert first is not None
    deny_notice = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    assert "SimAuth denied exec approval" in deny_notice.content

    second = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_requester",
            chat_id="oc_group_1",
            content="now just say hi",
            metadata={
                "msg_type": "text",
                "message_id": "om_exec_sim_replay_2",
                "_suppress_progress": True,
            },
        )
    )
    assert second is not None
    assert "no retry" in second.content
    assert provider.calls == 2
    assert target.exists()


def test_dangerous_mode_inherits_stronger_confirm_mode(tmp_path: Path) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=ExecApprovalProvider("rm -f /tmp/unused"),
        workspace=tmp_path,
        model="dummy/test-model",
        memory_window=20,
        exec_config=ExecToolConfig(
            approval_confirm_mode="sim_auth",
            approval_dangerous_mode="feishu_card",
            approval_sim_auth_url="https://sim-auth.local/verify",
        ),
    )

    assert loop._approval_mode("feishu", sender_id="ou_requester", risk_level="dangerous") == "sim_auth"


@pytest.mark.asyncio
async def test_exec_approval_dangerous_mode_can_use_sim_auth(
    monkeypatch,
    tmp_path: Path,
) -> None:
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
        exec_config=ExecToolConfig(
            approval_confirm_mode="none",
            approval_dangerous_mode="sim_auth",
            approval_sim_auth_url="https://sim-auth.local/verify",
        ),
    )

    async def _fake_decision(request):  # noqa: ANN001
        assert request.risk_level == "dangerous"
        return "deny", "dangerous action blocked by sim auth"

    monkeypatch.setattr(loop, "_request_sim_auth_decision", _fake_decision)

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_requester",
            chat_id="oc_group_1",
            content="run dangerous cleanup",
            metadata={
                "msg_type": "text",
                "message_id": "om_exec_sim_hd_1",
                "_suppress_progress": True,
            },
        )
    )

    assert response is not None
    assert "SimAuth verification" in response.content

    deny_notice = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    assert "SimAuth denied exec approval" in deny_notice.content
    assert "dangerous action blocked by sim auth" in deny_notice.content
    assert provider.calls == 1
