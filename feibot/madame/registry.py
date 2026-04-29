"""Registry models and persistence for Madame-managed agents."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

SUPPORTED_ROLES = {
    "manager",
    "agent",
    "secretary",
    "coder",
    "researcher",
    "family_helper",
    "chater",
}
SUPPORTED_MODES = {"agent", "chat"}
SUPPORTED_SLOT_STATUS = {"available", "assigned"}


def _dedupe_keep_order(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        item = str(raw or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


class CredentialSlot(BaseModel):
    """Named app credential slot used for dynamic agent allocation."""

    display_name: str
    app_id: str
    app_secret: str
    status: str = "available"
    assigned_runtime_id: str = ""

    @field_validator("display_name", "app_id", "app_secret", "status", "assigned_runtime_id", mode="before")
    @classmethod
    def _strip_text(cls, value: object) -> str:
        return str(value or "").strip()

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        normalized = value.strip().lower() or "available"
        if normalized not in SUPPORTED_SLOT_STATUS:
            raise ValueError("slot status must be one of: available, assigned")
        return normalized


class ManagedAgent(BaseModel):
    """Single managed agent record."""

    runtime_id: str
    mode: str = "agent"
    role: str = "agent"
    profile: str = ""

    launchd_label: str
    config_path: str
    workspace_path: str
    sessions_path: str = ""
    run_dir: str

    slot_name: str = ""
    app_id: str = ""
    app_secret: str = ""
    archived: bool = False
    archived_at: str = ""
    backup_path: str = ""

    skills: list[str] = Field(default_factory=list)
    tool_policy: str = "default"
    memory_mode: str = "default"
    skill_mode: str = "default"

    @field_validator(
        "runtime_id",
        "mode",
        "role",
        "profile",
        "launchd_label",
        "config_path",
        "workspace_path",
        "sessions_path",
        "run_dir",
        "slot_name",
        "app_id",
        "app_secret",
        "archived_at",
        "backup_path",
        mode="before",
    )
    @classmethod
    def _strip_text(cls, value: object) -> str:
        return str(value or "").strip()

    @field_validator("skills", mode="before")
    @classmethod
    def _normalize_list(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            parts = [x.strip() for x in value.split(",")]
            return _dedupe_keep_order(parts)
        if isinstance(value, list):
            return _dedupe_keep_order([str(x) for x in value])
        return []

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, value: str) -> str:
        normalized = value.strip().lower() or "agent"
        # Backward compatibility for older registry records.
        if normalized == "pure_chat":
            normalized = "chat"
        if normalized not in SUPPORTED_MODES:
            raise ValueError("unsupported mode; expected one of: agent, chat")
        return normalized

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        role = value.strip().lower() or "agent"
        if role not in SUPPORTED_ROLES:
            roles = ", ".join(sorted(SUPPORTED_ROLES))
            raise ValueError(f"Unsupported role '{value}'. Supported roles: {roles}")
        return role

    @model_validator(mode="after")
    def _fill_defaults(self) -> "ManagedAgent":
        if not self.profile:
            self.profile = self.role
        if not self.role:
            self.role = "chater" if self.mode == "chat" else "agent"
        return self


class AgentRegistry(BaseModel):
    """Top-level registry document."""

    version: int = 3
    updated_at: str = ""
    credential_pool: list[CredentialSlot] = Field(default_factory=list)
    agents: list[ManagedAgent] = Field(default_factory=list)

    @field_validator("updated_at", mode="before")
    @classmethod
    def _normalize_updated_at(cls, value: object) -> str:
        text = str(value or "").strip()
        return text or datetime.now().isoformat()

    @field_validator("credential_pool", mode="before")
    @classmethod
    def _normalize_pool(cls, value: object) -> list[object]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return []

    @model_validator(mode="after")
    def _validate_uniqueness(self) -> "AgentRegistry":
        agent_ids: set[str] = set()
        for agent in self.agents:
            if agent.runtime_id in agent_ids:
                raise ValueError(f"Duplicate runtime_id in registry: {agent.runtime_id}")
            agent_ids.add(agent.runtime_id)

        slot_names: set[str] = set()
        for slot in self.credential_pool:
            key = slot.display_name.lower()
            if key in slot_names:
                raise ValueError(f"Duplicate credential_pool display_name: {slot.display_name}")
            slot_names.add(key)

        self.updated_at = datetime.now().isoformat()
        return self

    def get_agent(self, runtime_id: str) -> ManagedAgent | None:
        key = str(runtime_id or "").strip()
        for agent in self.agents:
            if agent.runtime_id == key:
                return agent
        return None

    def upsert_agent(self, record: ManagedAgent) -> None:
        for idx, agent in enumerate(self.agents):
            if agent.runtime_id == record.runtime_id:
                self.agents[idx] = record
                self.updated_at = datetime.now().isoformat()
                return
        self.agents.append(record)
        self.updated_at = datetime.now().isoformat()

    def remove_agent(self, runtime_id: str) -> bool:
        key = str(runtime_id or "").strip()
        for idx, agent in enumerate(self.agents):
            if agent.runtime_id == key:
                del self.agents[idx]
                self.updated_at = datetime.now().isoformat()
                return True
        return False

    def get_pool_slot(self, display_name: str) -> CredentialSlot | None:
        # Normalize: lowercase, take first word, strip non-alphanumeric.
        key = re.sub(r"[^a-z0-9]+", "", str(display_name or "").strip().lower().split()[0])
        for slot in self.credential_pool:
            slot_key = re.sub(r"[^a-z0-9]+", "", slot.display_name.strip().lower().split()[0])
            if slot_key == key:
                return slot
        return None

    def upsert_pool_slot(self, slot: CredentialSlot) -> None:
        key = re.sub(r"[^a-z0-9]+", "", slot.display_name.strip().lower().split()[0])
        for idx, item in enumerate(self.credential_pool):
            item_key = re.sub(r"[^a-z0-9]+", "", item.display_name.strip().lower().split()[0])
            if item_key == key:
                self.credential_pool[idx] = slot
                self.updated_at = datetime.now().isoformat()
                return
        self.credential_pool.append(slot)
        self.updated_at = datetime.now().isoformat()

    def remove_pool_slot(self, display_name: str) -> bool:
        key = re.sub(r"[^a-z0-9]+", "", str(display_name or "").strip().lower().split()[0])
        for idx, slot in enumerate(self.credential_pool):
            slot_key = re.sub(r"[^a-z0-9]+", "", slot.display_name.strip().lower().split()[0])
            if slot_key == key:
                del self.credential_pool[idx]
                self.updated_at = datetime.now().isoformat()
                return True
        return False


class AgentRegistryStore:
    """Load/save helper for registry files."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()

    def load(self) -> AgentRegistry:
        if not self.path.exists():
            return AgentRegistry()
        raw = json.loads(self.path.read_text(encoding="utf-8"))

        # Backward-compatible key migration from legacy registry shape.
        if "credential_pool" not in raw and isinstance(raw.get("pool"), list):
            raw["credential_pool"] = raw.get("pool", [])
        return AgentRegistry.model_validate(raw)

    def save(self, registry: AgentRegistry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = registry.model_dump(mode="json")
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
