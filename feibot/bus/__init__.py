"""Message bus module for decoupled channel-agent communication."""

from feibot.bus.events import InboundMessage, OutboundMessage
from feibot.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
