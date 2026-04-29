import json
from pathlib import Path

from feibot.config.loader import load_config


def _write_config(path: Path, tools_cfg: dict[str, object] | None = None) -> Path:
    path.write_text(
        json.dumps(
            {
                "name": "feibot",
                "paths": {"workspace": "./workspace", "sessions": "./sessions"},
                "agents": {"defaults": {"model": "openai/gpt-4o"}},
                "tools": tools_cfg or {},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_load_config_reads_new_tool_security_fields(tmp_path: Path) -> None:
    config = load_config(
        _write_config(
            tmp_path / "config.json",
            {
                "writableDirs": ["/tmp/workspace", "/tmp/shared"],
                "allowedHosts": ["example.com", "buildbox.internal"],
                "exec": {"timeout": 120},
            },
        )
    )

    assert config.tools.writable_dirs == ["/tmp/workspace", "/tmp/shared"]
    assert config.tools.allowed_hosts == ["example.com", "buildbox.internal"]
    assert config.tools.exec.timeout == 120


def test_load_config_reads_skills_env(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "name": "feibot",
                "paths": {"workspace": "./workspace", "sessions": "./sessions"},
                "agents": {"defaults": {"model": "openai/gpt-4o"}},
                "skills": {
                    "env": {
                        "OPENAI_API_KEY": "sk-test",
                        "OPENAI_BASE_URL": "https://api.example.com/v1",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(path)
    assert config.skills.env["OPENAI_API_KEY"] == "sk-test"
    assert config.skills.env["OPENAI_BASE_URL"] == "https://api.example.com/v1"


def test_load_config_uses_agent_default_thresholds(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "name": "feibot",
                "paths": {"workspace": "./workspace", "sessions": "./sessions"},
                "agents": {"defaults": {"model": "openai/gpt-4o"}},
            }
        ),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.agents.defaults.max_tool_iterations == 100
    assert config.agents.defaults.max_consecutive_tool_errors == 10
