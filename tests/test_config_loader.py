import json
from pathlib import Path

from feibot.config.loader import load_config
from feibot.config.schema import ExecToolConfig


def _write_config(path: Path, exec_cfg: dict[str, object]) -> Path:
    path.write_text(
        json.dumps(
            {
                "name": "feibot",
                "paths": {"workspace": "./workspace", "sessions": "./sessions"},
                "agents": {"defaults": {"model": "openai/gpt-4o"}},
                "tools": {"exec": exec_cfg},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_load_config_migrates_legacy_approval_modes(tmp_path: Path) -> None:
    config = load_config(
        _write_config(
            tmp_path / "config.json",
            {
                "approvalEnabled": True,
                "approvalConfirmMode": "none",
                "approvalDangerousMode": "feishu_card",
            },
        )
    )

    assert config.tools.exec.approval_risk_level == "dangerous"


def test_load_config_migrates_legacy_text_approval_mode(tmp_path: Path) -> None:
    config = load_config(
        _write_config(
            tmp_path / "config.json",
            {
                "approvalEnabled": True,
                "approvalMode": "text",
            },
        )
    )

    assert config.tools.exec.approval_risk_level == "confirm"


def test_exec_tool_config_accepts_legacy_feishu_card_value() -> None:
    config = ExecToolConfig(approval_risk_level="feishu_card")

    assert config.approval_risk_level == "confirm"
