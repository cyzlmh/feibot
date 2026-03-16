"""Configuration loading utilities."""

import json
from pathlib import Path
from typing import Any

from feibot.config.schema import Config


def load_config(config_path: Path) -> Config:
    """
    Load configuration from file.
    
    Args:
        config_path: Path to config file.
    
    Returns:
        Loaded configuration object.
    """
    path = config_path
    
    if not path.exists():
        raise ValueError(f"Config file not found: {path}")

    try:
        with open(path) as f:
            data = json.load(f)
        skills_env = _extract_skills_env(data)
        data = _migrate_config(data)
        normalized = convert_keys(data)
        if skills_env is not None:
            normalized.setdefault("skills", {})["env"] = skills_env
        return Config.model_validate(normalized)
    except (json.JSONDecodeError, ValueError) as e:
        raise ValueError(f"Failed to parse config: {e}") from e


def _extract_skills_env(data: dict[str, Any]) -> dict[str, Any] | None:
    """Extract skills.env map without key normalization."""
    skills = data.get("skills")
    if not isinstance(skills, dict):
        return None
    env = skills.get("env")
    if not isinstance(env, dict):
        return None
    return {str(k): v for k, v in env.items() if str(k).strip()}


def save_config(config: Config, config_path: Path) -> None:
    """
    Save configuration to file.
    
    Args:
        config: Configuration to save.
        config_path: Path to save to.
    """
    path = config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    
    # Convert to camelCase format
    data = config.model_dump()
    data = convert_to_camel(data)
    
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")

    if "approvalConfirmMode" not in exec_cfg and "approvalMode" in exec_cfg:
        exec_cfg["approvalConfirmMode"] = exec_cfg.pop("approvalMode")
    if "approvalDangerousMode" not in exec_cfg and "approvalHardDangerMode" in exec_cfg:
        exec_cfg["approvalDangerousMode"] = exec_cfg.pop("approvalHardDangerMode")

    if "approvalRiskLevel" not in exec_cfg:
        exec_cfg["approvalRiskLevel"] = _infer_approval_risk_level(exec_cfg)
    else:
        exec_cfg["approvalRiskLevel"] = _normalize_approval_risk_level(exec_cfg.get("approvalRiskLevel"))

    exec_cfg.pop("approvalConfirmMode", None)
    exec_cfg.pop("approvalDangerousMode", None)
    return data


def _normalize_approval_risk_level(value: Any) -> str:
    """Normalize approval risk level from current or legacy config values."""
    level = str(value or "").strip().lower()
    if level in {"", "none", "dangerous", "confirm"}:
        return level
    if level in {"text", "feishu_card"}:
        return "confirm"
    return ""


def _normalize_legacy_approval_mode(value: Any) -> str:
    """Normalize legacy approval mode fields used before approvalRiskLevel."""
    mode = str(value or "").strip().lower()
    if mode == "text":
        mode = "feishu_card"
    if mode in {"", "none", "feishu_card"}:
        return mode
    return ""


def _infer_approval_risk_level(exec_cfg: dict[str, Any]) -> str:
    """Infer approvalRiskLevel from older confirm/dangerous mode settings."""
    confirm_mode = _normalize_legacy_approval_mode(exec_cfg.get("approvalConfirmMode"))
    dangerous_mode = _normalize_legacy_approval_mode(exec_cfg.get("approvalDangerousMode"))
    if confirm_mode == "feishu_card":
        return "confirm"
    if dangerous_mode == "feishu_card":
        return "dangerous"
    return ""


def convert_keys(data: Any) -> Any:
    """Convert camelCase keys to snake_case for Pydantic."""
    if isinstance(data, dict):
        return {camel_to_snake(k): convert_keys(v) for k, v in data.items()}
    if isinstance(data, list):
        return [convert_keys(item) for item in data]
    return data


def convert_to_camel(data: Any) -> Any:
    """Convert snake_case keys to camelCase."""
    if isinstance(data, dict):
        return {snake_to_camel(k): convert_to_camel(v) for k, v in data.items()}
    if isinstance(data, list):
        return [convert_to_camel(item) for item in data]
    return data


def camel_to_snake(name: str) -> str:
    """Convert camelCase to snake_case."""
    result = []
    for i, char in enumerate(name):
        if char.isupper() and i > 0:
            result.append("_")
        result.append(char.lower())
    return "".join(result)


def snake_to_camel(name: str) -> str:
    """Convert snake_case to camelCase."""
    components = name.split("_")
    return components[0] + "".join(x.title() for x in components[1:])
