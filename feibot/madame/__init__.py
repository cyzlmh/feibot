"""Madame control plane for multi-agent lifecycle and capability orchestration."""

from feibot.madame.controller import AgentMadameController
from feibot.madame.registry import (
    AgentRegistry,
    AgentRegistryStore,
    CredentialSlot,
    ManagedAgent,
)

__all__ = [
    "AgentMadameController",
    "AgentRegistry",
    "AgentRegistryStore",
    "CredentialSlot",
    "ManagedAgent",
]
