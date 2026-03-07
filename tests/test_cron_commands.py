import json
from pathlib import Path

from typer.testing import CliRunner

from feibot.cli.commands import app

runner = CliRunner()


def _write_config(tmp_path: Path) -> Path:
    config = {
        "name": "test-bot",
        "paths": {
            "workspace": str(tmp_path / "workspace"),
            "sessions": str(tmp_path / "sessions"),
        },
        "agents": {
            "defaults": {
                "model": "dummy-model",
            }
        },
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def test_cron_add_rejects_invalid_timezone(tmp_path) -> None:
    config_path = _write_config(tmp_path)

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "cron",
            "add",
            "--name",
            "demo",
            "--message",
            "hello",
            "--cron",
            "0 9 * * *",
            "--tz",
            "America/Vancovuer",
        ],
    )

    assert result.exit_code == 1
    assert "Error: unknown timezone 'America/Vancovuer'" in result.stdout
    assert not (tmp_path / "workspace" / "cron" / "jobs.json").exists()


def test_cron_add_creates_job_file(tmp_path) -> None:
    config_path = _write_config(tmp_path)

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "cron",
            "add",
            "--name",
            "demo",
            "--message",
            "hello",
            "--every",
            "60",
        ],
    )

    assert result.exit_code == 0
    assert "Added job 'demo'" in result.stdout

    jobs_path = tmp_path / "workspace" / "cron" / "jobs.json"
    assert jobs_path.exists()
    payload = json.loads(jobs_path.read_text(encoding="utf-8"))
    assert len(payload["jobs"]) == 1
    assert payload["jobs"][0]["name"] == "demo"


def test_cron_add_deliver_defaults_to_feishu_channel(tmp_path) -> None:
    config_path = _write_config(tmp_path)

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "cron",
            "add",
            "--name",
            "deliver-demo",
            "--message",
            "hello",
            "--every",
            "60",
            "--deliver",
            "--to",
            "oc_test_chat",
        ],
    )

    assert result.exit_code == 0
    jobs_path = tmp_path / "workspace" / "cron" / "jobs.json"
    payload = json.loads(jobs_path.read_text(encoding="utf-8"))
    assert payload["jobs"][0]["payload"]["channel"] == "feishu"
    assert payload["jobs"][0]["payload"]["to"] == "oc_test_chat"


def test_cron_add_deliver_requires_to(tmp_path) -> None:
    config_path = _write_config(tmp_path)

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "cron",
            "add",
            "--name",
            "deliver-missing",
            "--message",
            "hello",
            "--every",
            "60",
            "--deliver",
        ],
    )

    assert result.exit_code == 1
    assert "Error: --to is required when --deliver is set" in result.stdout


def test_cron_add_rejects_non_feishu_channel(tmp_path) -> None:
    config_path = _write_config(tmp_path)

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "cron",
            "add",
            "--name",
            "invalid-channel",
            "--message",
            "hello",
            "--every",
            "60",
            "--channel",
            "cli",
        ],
    )

    assert result.exit_code == 1
    assert "Error: --channel only supports 'feishu'" in result.stdout
