"""Message tool for sending text messages to users."""

from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from feibot.agent.tools.base import Tool
from feibot.bus.events import OutboundMessage


class MessageTool(Tool):
    """Tool to send messages to users on chat channels."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
    ):
        self._send_callback = send_callback
        self._default_channel_ctx: ContextVar[str] = ContextVar(
            "message_default_channel",
            default=default_channel,
        )
        self._default_chat_id_ctx: ContextVar[str] = ContextVar(
            "message_default_chat_id",
            default=default_chat_id,
        )
        self._sent_in_turn_ctx: ContextVar[bool] = ContextVar(
            "message_sent_in_turn",
            default=False,
        )
        self._finish_requested_ctx: ContextVar[bool] = ContextVar(
            "message_finish_requested_in_turn",
            default=False,
        )

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set per-request routing defaults."""
        self._default_channel_ctx.set(channel)
        self._default_chat_id_ctx.set(chat_id)

    def start_turn(self) -> None:
        """Reset per-turn send tracking."""
        self._sent_in_turn = False
        self._finish_requested = False

    @property
    def _sent_in_turn(self) -> bool:
        """Whether message tool sent at least one message in current turn/task context."""
        return self._sent_in_turn_ctx.get()

    @_sent_in_turn.setter
    def _sent_in_turn(self, value: bool) -> None:
        self._sent_in_turn_ctx.set(bool(value))

    @property
    def finish_requested(self) -> bool:
        """Whether message tool requested loop finish in current turn."""
        return self._finish_requested_ctx.get()

    @property
    def _finish_requested(self) -> bool:
        return self._finish_requested_ctx.get()

    @_finish_requested.setter
    def _finish_requested(self, value: bool) -> None:
        self._finish_requested_ctx.set(bool(value))

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return "Send a text message to the user in the current chat context."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The message content to send.",
                },
                "channel": {
                    "type": "string",
                    "description": "Optional target channel override.",
                },
                "chat_id": {
                    "type": "string",
                    "description": "Optional target chat ID override.",
                },
                "finish": {
                    "type": "boolean",
                    "description": "If true, request the agent loop to stop after this message.",
                },
            },
            "required": ["content"],
        }

    async def execute(
        self,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        finish: bool = False,
        **kwargs: Any,
    ) -> str:
        target_channel = (channel or self._default_channel_ctx.get() or "").strip()
        target_chat_id = (chat_id or self._default_chat_id_ctx.get() or "").strip()

        if not target_channel or not target_chat_id:
            return "Error: No target channel/chat specified"
        if not self._send_callback:
            return "Error: Message sending not configured"

        msg = OutboundMessage(
            channel=target_channel,
            chat_id=target_chat_id,
            content=content,
        )
        try:
            await self._send_callback(msg)
            self._sent_in_turn = True
            if finish:
                self._finish_requested = True
            return f"Message sent to {target_channel}:{target_chat_id}"
        except Exception as e:
            return f"Error sending message: {str(e)}"
