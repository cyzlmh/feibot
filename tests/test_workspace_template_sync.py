from pathlib import Path

from feibot.utils.helpers import sync_workspace_templates


def test_sync_workspace_templates_creates_missing_files(tmp_path: Path) -> None:
    added = sync_workspace_templates(tmp_path, silent=True)

    assert "AGENTS.md" in added
    assert "cron/jobs.json" in added
    assert "memory/MEMORY.md" in added
    assert "memory/HISTORY.md" in added
    assert (tmp_path / "skills").is_dir()
    assert (tmp_path / "cron" / "jobs.json").exists()


def test_sync_workspace_templates_does_not_overwrite_existing_files(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.parent.mkdir(parents=True, exist_ok=True)
    agents.write_text("custom agents", encoding="utf-8")
    jobs = tmp_path / "cron" / "jobs.json"
    jobs.parent.mkdir(parents=True, exist_ok=True)
    jobs.write_text('{"version": 1, "jobs": []}', encoding="utf-8")

    sync_workspace_templates(tmp_path, silent=True)

    assert agents.read_text(encoding="utf-8") == "custom agents"
    assert jobs.read_text(encoding="utf-8") == '{"version": 1, "jobs": []}'


def test_sync_workspace_templates_does_not_recreate_jobs_when_cron_dir_exists(tmp_path: Path) -> None:
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)

    added = sync_workspace_templates(tmp_path, silent=True)

    assert "cron/jobs.json" not in added
    assert not (cron_dir / "jobs.json").exists()
