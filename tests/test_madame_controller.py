import json
from pathlib import Path

from feibot.madame.controller import AgentMadameController
from feibot.madame.registry import ManagedAgent


def _make_controller(tmp_path: Path) -> AgentMadameController:
    workspace = tmp_path / "madame-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    controller = AgentMadameController(
        workspace=workspace,
        repo_dir=repo_dir,
        registry_path=tmp_path / "madame" / "agents_registry.json",
        madame_runtime_id="madame-gazelle",
        manage_script=None,
        base_dir_template=str(tmp_path / "agents" / "{runtime_id}"),
    )
    madame_cfg = tmp_path / "madame" / "config.json"
    madame_cfg.parent.mkdir(parents=True, exist_ok=True)
    madame_cfg.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "openai/gpt-4o-mini"}},
                "providers": {
                    "openai": {"apiKey": "sk-madame-openai", "apiBase": "https://openai.example/v1"},
                    "anthropic": {"apiKey": "sk-madame-anthropic"},
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return controller


def _install_shared_skill(tmp_path: Path, monkeypatch, name: str) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    skill_dir = tmp_path / "shared" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    return skill_dir


def test_create_agent_writes_registry_and_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    controller = _make_controller(tmp_path)
    controller.execute('pool add --name "Candy Cat" --app-id "cli_xxx" --app-secret "sec_xxx"')
    reply = controller.execute('create --name "Candy Cat" --mode agent')
    assert "Created agent '" in reply

    registry = controller.store.load()
    agent = next((x for x in registry.agents if x.runtime_id == "candy" and x.role != "manager"), None)
    assert agent is not None
    assert agent.runtime_id == "candy"
    assert agent.role == "agent"
    assert agent.mode == "agent"
    assert agent.skills == []
    slot = registry.get_pool_slot("candy")
    assert slot is not None
    assert slot.status == "assigned"
    assert slot.assigned_runtime_id == agent.runtime_id

    cfg = json.loads(Path(agent.config_path).read_text(encoding="utf-8"))
    assert cfg["name"] == agent.runtime_id
    assert cfg["channels"]["feishu"]["appId"] == "cli_xxx"
    assert cfg["channels"]["feishu"]["appSecret"] == "sec_xxx"
    assert cfg["madame"]["enabled"] is False
    assert cfg["tools"]["writableDirs"] == [
        str((tmp_path / "agents" / "candy" / "workspace").resolve()),
        str((tmp_path / "shared" / "workdir").resolve()),
    ]
    assert cfg["skills"]["env"] == {
        "FEIBOT_AGENT_BASE_DIR": str(tmp_path / "agents" / "candy"),
        "FEIBOT_SHARED_WORKDIR": str((tmp_path / "shared" / "workdir").resolve()),
        "FEIBOT_DISABLE_BUILTIN_SKILLS": "1",
    }
    assert (tmp_path / "shared" / "workdir").is_dir()


def test_create_agent_inherits_providers_from_madame_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    controller = _make_controller(tmp_path)
    madame_config_path = tmp_path / "madame" / "config.json"
    madame_config_path.parent.mkdir(parents=True, exist_ok=True)
    shared_dir = tmp_path / "shared-config"
    manager_base_dir = madame_config_path.parent
    providers = {
        "openai": {"apiKey": "sk-openai-test", "apiBase": "https://openai.example/v1"},
        "anthropic": {"apiKey": "sk-ant-test"},
    }
    madame_config_path.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "openai/gpt-4o-mini"}},
                "providers": providers,
                "tools": {
                    "writableDirs": [str(manager_base_dir), str(shared_dir)],
                    "allowedHosts": ["buildbox.internal"],
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    controller.execute('pool add --name "Candy Cat" --app-id "cli_xxx" --app-secret "sec_xxx"')
    controller.execute('create --name "Candy Cat" --mode agent')

    registry = controller.store.load()
    agent = next((x for x in registry.agents if x.runtime_id == "candy" and x.role != "manager"), None)
    assert agent is not None

    cfg = json.loads(Path(agent.config_path).read_text(encoding="utf-8"))
    assert cfg["agents"]["defaults"]["model"] == "openai/gpt-4o-mini"
    assert cfg["providers"] == providers
    assert cfg["tools"]["writableDirs"] == [
        str((tmp_path / "agents" / "candy" / "workspace").resolve()),
        str(shared_dir.resolve()),
        str((tmp_path / "shared" / "workdir").resolve()),
    ]
    assert cfg["tools"]["allowedHosts"] == ["buildbox.internal"]


def test_skills_assign_links_shared_skill(tmp_path: Path, monkeypatch) -> None:
    shared_skill = _install_shared_skill(tmp_path, monkeypatch, "docx")
    controller = _make_controller(tmp_path)
    controller.execute('pool add --name "Candy Cat" --app-id "cli_candy" --app-secret "sec_candy"')
    controller.execute('create --name "Candy Cat" --mode agent')

    registry = controller.store.load()
    agent = next((x for x in registry.agents if x.runtime_id == "candy" and not x.archived), None)
    assert agent is not None

    assert "docx" in controller.execute("skills hub list")
    add_reply = controller.execute("skills agent add candy docx")
    assert "- assigned: docx" in add_reply
    assert "- linked: docx" in add_reply

    skill_link = Path(agent.workspace_path) / "skills" / "docx"
    assert skill_link.is_symlink()
    assert skill_link.resolve() == shared_skill.resolve()

    show = controller.execute("skills agent show candy")
    assert "- assigned: docx" in show
    assert "- linked: docx" in show


def test_skills_preserve_local_workspace_dirs_on_conflict(tmp_path: Path, monkeypatch) -> None:
    _install_shared_skill(tmp_path, monkeypatch, "docx")
    controller = _make_controller(tmp_path)
    controller.execute('pool add --name "Candy Cat" --app-id "cli_candy" --app-secret "sec_candy"')
    controller.execute('create --name "Candy Cat" --mode agent')

    registry = controller.store.load()
    agent = next((x for x in registry.agents if x.runtime_id == "candy" and not x.archived), None)
    assert agent is not None
    local_skill = Path(agent.workspace_path) / "skills" / "docx"
    local_skill.mkdir(parents=True, exist_ok=True)
    (local_skill / "SKILL.md").write_text("# local docx\n", encoding="utf-8")

    reply = controller.execute("skills agent add candy docx")

    assert "- conflicts: docx" in reply
    assert local_skill.exists()
    assert not local_skill.is_symlink()
    assert "- conflicts: docx" in controller.execute("skills agent show candy")


def test_skills_add_fails_for_nonexistent_skill(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    controller = _make_controller(tmp_path)
    controller.execute('pool add --name "Candy Cat" --app-id "cli_candy" --app-secret "sec_candy"')
    controller.execute('create --name "Candy Cat" --mode agent')

    reply = controller.execute("skills agent add candy nonexistent-skill")
    assert "Skills not found in hub: nonexistent-skill" in reply


def test_pool_remove_rejects_assigned_slot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    controller = _make_controller(tmp_path)
    controller.execute('pool add --name "Candy Cat" --app-id "cli_xxx" --app-secret "sec_xxx"')
    controller.execute('create --name "Candy Cat" --mode agent')

    reply = controller.execute('pool remove "Candy Cat"')
    assert "cannot remove" in reply


def test_list_agents_renders_markdown_table(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    controller = _make_controller(tmp_path)
    controller.execute('pool add --name "Candy Cat" --app-id "cli_xxx" --app-secret "sec_xxx"')
    controller.execute('create --name "Candy Cat" --mode agent')

    registry = controller.store.load()
    agent = next((x for x in registry.agents if x.runtime_id == "candy"), None)
    assert agent is not None

    reply = controller.execute("list")
    assert "| Runtime ID | Mode | Status | Slot | Service |" in reply
    assert f"| {agent.runtime_id} | agent | active | candy |" in reply
    assert "| Total | Available | Assigned |" in reply


def test_create_agent_runtime_id_has_no_timestamp_suffix(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    controller = _make_controller(tmp_path)
    controller.execute('pool add --name "Candy Cat" --app-id "cli_1" --app-secret "sec_1"')
    controller.execute('pool add --name "Candycat2" --app-id "cli_2" --app-secret "sec_2"')

    controller.execute('create --name "Candy Cat" --mode agent')
    controller.execute('create --name "Candycat2" --mode agent')

    registry = controller.store.load()
    runtime_ids = sorted(
        [x.runtime_id for x in registry.agents if x.runtime_id in {"candy", "candycat"}]
    )
    assert runtime_ids == ["candy", "candycat"]


def test_restart_all_only_restarts_loaded_non_manager_agents(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    controller = _make_controller(tmp_path)
    controller.execute('pool add --name "Candy Cat" --app-id "cli_candy" --app-secret "sec_candy"')
    controller.execute('pool add --name "Suzy Sheep" --app-id "cli_suzy" --app-secret "sec_suzy"')
    controller.execute('create --name "Candy Cat" --mode agent')
    controller.execute('create --name "Suzy Sheep" --mode agent')

    registry = controller.store.load()
    registry.upsert_agent(
        ManagedAgent(
            runtime_id="madame",
            mode="agent",
            role="manager",
            profile="manager",
            launchd_label="ai.madame.gateway",
            config_path=str(tmp_path / "madame" / "config.json"),
            workspace_path=str(tmp_path / "madame" / "workspace"),
            sessions_path=str(tmp_path / "madame" / "sessions"),
            run_dir=str(tmp_path / "madame" / "run"),
        )
    )
    controller.store.save(registry)

    restarted: list[str] = []
    service_states = {
        "ai.candy.gateway": "running(pid=101)",
        "ai.suzy.gateway": "not_loaded",
        "ai.madame.gateway": "running(pid=1)",
    }

    def fake_launchd_state(label: str) -> str:
        return service_states[label]

    def fake_run_manage_script(agent: ManagedAgent, action: str) -> str:
        restarted.append(f"{agent.runtime_id}:{action}")
        return f"{action}:{agent.runtime_id}"

    monkeypatch.setattr(controller, "_launchd_state", fake_launchd_state)
    monkeypatch.setattr(controller, "_run_manage_script", fake_run_manage_script)

    reply = controller.execute("restart all")

    assert restarted == ["candy:restart"]
    assert "- restarted: candy (running(pid=101))" in reply
    assert "- skipped: suzy (not_loaded)" in reply
    assert "- failed: (none)" in reply
