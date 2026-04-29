from pathlib import Path

import pytest

from feibot.madame.controller import AgentMadameController
from feibot.madame.registry import ManagedAgent


def _make_controller(tmp_path: Path) -> AgentMadameController:
    workspace = tmp_path / "madame-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    return AgentMadameController(
        workspace=workspace,
        repo_dir=repo_dir,
        registry_path=tmp_path / "madame" / "agents_registry.json",
        madame_runtime_id="madame",
        manage_script=None,
        base_dir_template=str(tmp_path / "agents" / "{runtime_id}"),
    )


def test_help_text_uses_chat_mode_label(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path)
    reply = controller.execute("help")
    assert "<agent|chat>" in reply
    assert "/agent restart all" in reply


def test_registry_rejects_unsupported_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported mode; expected one of: agent, chat"):
        ManagedAgent(
            runtime_id="chat-agent",
            display_name="Chat Agent",
            mode="unknown",
            role="agent",
            profile="agent",
            launchd_label="ai.chat-agent.gateway",
            config_path=str(tmp_path / "chat-agent" / "config.json"),
            workspace_path=str(tmp_path / "chat-agent" / "workspace"),
            sessions_path=str(tmp_path / "chat-agent" / "sessions"),
            run_dir=str(tmp_path / "chat-agent" / "run"),
        )


def test_registry_normalizes_legacy_pure_chat_mode(tmp_path: Path) -> None:
    agent = ManagedAgent(
        runtime_id="legacy-chat-agent",
        display_name="Legacy Chat Agent",
        mode="pure_chat",
        role="chater",
        profile="chater",
        launchd_label="ai.legacy-chat-agent.gateway",
        config_path=str(tmp_path / "legacy-chat-agent" / "config.json"),
        workspace_path=str(tmp_path / "legacy-chat-agent" / "workspace"),
        sessions_path=str(tmp_path / "legacy-chat-agent" / "sessions"),
        run_dir=str(tmp_path / "legacy-chat-agent" / "run"),
    )
    assert agent.mode == "chat"
