import json
from pathlib import Path

from typer.testing import CliRunner

from feibot.cli.commands import app

runner = CliRunner()


def _write_fake_repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repo"
    script_dir = repo_dir / "feibot" / "skills" / "feibot-ops" / "scripts"
    script_dir.mkdir(parents=True, exist_ok=True)
    script_path = script_dir / "manage.sh"
    script_path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    script_path.chmod(0o755)
    return repo_dir


def test_madame_init_creates_runtime_and_pool(tmp_path: Path) -> None:
    repo_dir = _write_fake_repo(tmp_path)
    madame_dir = tmp_path / "madame"

    result = runner.invoke(
        app,
        [
            "madame",
            "init",
            "--repo-dir",
            str(repo_dir),
            "--madame-dir",
            str(madame_dir),
            "--app-id",
            "cli_madame",
            "--app-secret",
            "sec_madame",
            "--pool-slot",
            "Candy Cat=cli_candy:sec_candy",
            "--pool-slot",
            "Zoe Zebra=cli_zoe:sec_zoe",
        ],
    )

    assert result.exit_code == 0

    config_path = madame_dir / "config.json"
    registry_path = madame_dir / "agents_registry.json"
    ops_path = madame_dir / "ops" / "manage.sh"

    assert config_path.exists()
    assert registry_path.exists()
    assert ops_path.exists()
    assert (madame_dir / "shared" / "workdir").is_dir()
    assert (madame_dir / "shared" / "skills" / "active").is_dir()
    assert (madame_dir / "shared" / "skills" / "inactive").is_dir()

    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    assert cfg["madame"]["runtimeId"] == "madame"
    assert cfg["madame"]["manageScript"] == str(ops_path)
    assert cfg["tools"]["writableDirs"] == [str(madame_dir.resolve())]
    assert cfg["tools"]["allowedHosts"] == []
    assert cfg["skills"]["env"] == {
        "FEIBOT_SHARED_WORKDIR": str((madame_dir / "shared" / "workdir").resolve()),
    }
    wrapper = ops_path.read_text(encoding="utf-8")
    assert f'FEIBOT_REPO_DIR="${{FEIBOT_REPO_DIR:-{repo_dir}}}"' in wrapper
    assert f'FEIBOT_CONFIG_FILE="${{FEIBOT_CONFIG_FILE:-{config_path}}}"' in wrapper
    assert f'FEIBOT_RUN_DIR="${{FEIBOT_RUN_DIR:-{madame_dir / "run"}}}"' in wrapper
    assert 'FEIBOT_LAUNCHD_LABEL="${FEIBOT_LAUNCHD_LABEL:-ai.madame.gateway}"' in wrapper
    assert f'exec "{repo_dir / "feibot" / "skills" / "feibot-ops" / "scripts" / "manage.sh"}" "$@"' in wrapper

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert len(registry["credential_pool"]) == 2
    assert any(x["display_name"] == "Candy Cat" for x in registry["credential_pool"])


def test_madame_pool_commands_not_exposed_in_cli(tmp_path: Path) -> None:
    repo_dir = _write_fake_repo(tmp_path)
    madame_dir = tmp_path / "madame"

    init = runner.invoke(
        app,
        [
            "madame",
            "init",
            "--repo-dir",
            str(repo_dir),
            "--madame-dir",
            str(madame_dir),
            "--app-id",
            "cli_madame",
            "--app-secret",
            "sec_madame",
        ],
    )
    assert init.exit_code == 0

    pool = runner.invoke(
        app,
        [
            "madame",
            "pool",
            "list",
        ],
    )
    assert pool.exit_code != 0
    assert "No such command 'pool'" in pool.output


def test_cli_only_exposes_madame_namespace() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "madame" in result.output
    assert "gateway" not in result.output
    assert "agent" not in result.output
    assert "cron" not in result.output
    assert "provider" not in result.output
    assert "wechat" not in result.output
