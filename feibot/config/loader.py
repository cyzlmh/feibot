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
        data = _migrate_config(data)
        return Config.model_validate(convert_keys(data))
    except (json.JSONDecodeError, ValueError) as e:
        raise ValueError(f"Failed to parse config: {e}") from e


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
    for key in ("approvalConfirmMode", "approvalDangerousMode"):
        if str(exec_cfg.get(key) or "").strip().lower() == "text":
            exec_cfg[key] = "feishu_card"
    return data


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
