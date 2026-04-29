from pathlib import Path

from feibot.madame.registry import AgentRegistryStore, CredentialSlot, ManagedAgent


def test_registry_store_roundtrip(tmp_path: Path) -> None:
    store = AgentRegistryStore(tmp_path / "madame" / "agents_registry.json")
    registry = store.load()
    assert registry.agents == []
    registry.upsert_pool_slot(
        CredentialSlot(
            display_name="Candy Cat",
            app_id="cli_123",
            app_secret="secret_abc",
            status="available",
        )
    )
    registry.upsert_agent(
        ManagedAgent(
            runtime_id="george",
            mode="agent",
            role="secretary",
            profile="secretary",
            launchd_label="ai.george.gateway",
            config_path=str(tmp_path / "george" / "config.json"),
            workspace_path=str(tmp_path / "george" / "workspace"),
            sessions_path=str(tmp_path / "george" / "sessions"),
            run_dir=str(tmp_path / "george" / "run"),
            app_id="cli_123",
            app_secret="secret_abc",
            skills=["docx", "feishu-docx-wiki"],
        )
    )
    store.save(registry)

    loaded = store.load()
    assert len(loaded.agents) == 1
    agent = loaded.get_agent("george")
    assert agent is not None
    assert agent.runtime_id == "george"
    assert agent.skills == ["docx", "feishu-docx-wiki"]
    assert loaded.get_pool_slot("Candy Cat") is not None
