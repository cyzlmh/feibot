from pathlib import Path

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


def test_chat_mode_uses_minimal_profile_and_web_only_tools(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path)
    agent = ManagedAgent(
        runtime_id="pedropony",
        display_name="PedroPony",
        mode="chat",
        role="chater",
        profile="chater",
        launchd_label="ai.pedropony.gateway",
        config_path=str(tmp_path / "pedropony" / "config.json"),
        workspace_path=str(tmp_path / "pedropony" / "workspace"),
        sessions_path=str(tmp_path / "pedropony" / "sessions"),
        run_dir=str(tmp_path / "pedropony" / "run"),
        app_id="cli_test",
        app_secret="secret_test",
    )
    payload = controller._build_config_payload(
        agent=agent,
        model="openai/gpt-4o-mini",
        sessions_path=tmp_path / "pedropony" / "sessions",
        workspace_path=tmp_path / "pedropony" / "workspace",
        allow_from=[],
        providers={"openai": {"apiKey": "sk-test"}},
    )

    defaults = payload["agents"]["defaults"]
    assert defaults["maxToolIterations"] == 100
    assert defaults["maxConsecutiveToolErrors"] == 10
    assert defaults["memoryWindow"] == 0
    assert defaults["disableTools"] is False
    assert defaults["disableSkills"] is True
    assert defaults["disableLongTermMemory"] is True
    assert payload["tools"]["writableDirs"] == [
        str((tmp_path / "pedropony" / "workspace").resolve()),
        str((tmp_path / "shared" / "workdir").resolve()),
    ]
    assert payload["tools"]["allowedHosts"] == []

    disabled_tools = set(payload["tools"]["disabledTools"])
    assert "web_search" not in disabled_tools
    assert "web_fetch" not in disabled_tools
    assert "exec" in disabled_tools
    assert "read_file" in disabled_tools
    assert "write_file" in disabled_tools
    assert payload["skills"]["env"] == {
        "FEIBOT_AGENT_BASE_DIR": str(tmp_path / "pedropony"),
        "FEIBOT_SHARED_WORKDIR": str((tmp_path / "shared" / "workdir").resolve()),
        "FEIBOT_DISABLE_BUILTIN_SKILLS": "1",
    }
