"""Feishu/Lark channel implementation using lark-oapi SDK with WebSocket long connection."""

import asyncio
import json
import re
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

from loguru import logger

from feibot.bus.events import OutboundMessage
from feibot.bus.queue import MessageBus
from feibot.channels.base import BaseChannel
from feibot.config.schema import FeishuConfig

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest,
        CreateMessageRequestBody,
        CreateMessageReactionRequest,
        CreateMessageReactionRequestBody,
        Emoji,
        GetMessageRequest,
        GetMessageResourceRequest,
        P2ImMessageReceiveV1,
    )
    FEISHU_AVAILABLE = True
except ImportError:
    FEISHU_AVAILABLE = False
    lark = None
    Emoji = None
    GetMessageRequest = None
    GetMessageResourceRequest = None
    P2CardActionTriggerResponse = None
    CallBackCard = None
    CallBackToast = None

if FEISHU_AVAILABLE:
    try:
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            CallBackCard,
            CallBackToast,
            P2CardActionTrigger,
            P2CardActionTriggerResponse,
        )
    except ImportError:
        P2CardActionTriggerResponse = None
        CallBackCard = None
        CallBackToast = None

# Message type display mapping
MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}

MAX_QUOTED_PREVIEW_CHARS = 1200
MAX_MERGE_FORWARD_PREVIEW_ITEMS = 6
MAX_MERGE_FORWARD_LINE_CHARS = 240


class FeishuChannel(BaseChannel):
    """
    Feishu/Lark channel using WebSocket long connection.
    
    Uses WebSocket to receive events - no public IP or webhook required.
    
    Requires:
    - App ID and App Secret from Feishu Open Platform
    - Bot capability enabled
    - Event subscription enabled (im.message.receive_v1)
    """
    
    name = "feishu"
    
    def __init__(self, config: FeishuConfig, bus: MessageBus, workspace_dir: Path | None = None):
        super().__init__(config, bus)
        self.config: FeishuConfig = config
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()  # Ordered dedup cache
        self._loop: asyncio.AbstractEventLoop | None = None
        self._workspace_dir = workspace_dir.expanduser().resolve() if workspace_dir else None
    
    async def start(self) -> None:
        """Start the Feishu bot with WebSocket long connection."""
        if not FEISHU_AVAILABLE:
            logger.error("Feishu SDK not installed. Run: pip install lark-oapi")
            return
        
        if not self.config.app_id or not self.config.app_secret:
            logger.error("Feishu app_id and app_secret not configured")
            return
        
        self._running = True
        self._loop = asyncio.get_running_loop()
        
        # Create Lark client for sending messages
        self._client = lark.Client.builder() \
            .app_id(self.config.app_id) \
            .app_secret(self.config.app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()
        
        # Create event handler (only register message receive, ignore other events)
        builder = lark.EventDispatcherHandler.builder(
            self.config.encrypt_key or "",
            self.config.verification_token or "",
        ).register_p2_im_message_receive_v1(
            self._on_message_sync
        )
        if hasattr(builder, "register_p2_card_action_trigger"):
            builder = builder.register_p2_card_action_trigger(self._on_card_action_sync)
        event_handler = builder.build()
        
        # Create WebSocket client for long connection
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO
        )
        
        # Start WebSocket client in a separate thread with reconnect loop
        def run_ws():
            while self._running:
                try:
                    self._ws_client.start()
                except Exception as e:
                    logger.warning(f"Feishu WebSocket error: {e}")
                if self._running:
                    import time; time.sleep(5)
        
        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()
        
        logger.info("Feishu bot started with WebSocket long connection")
        logger.info("No public IP required - using WebSocket to receive events")
        
        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)
    
    async def stop(self) -> None:
        """Stop the Feishu bot."""
        self._running = False
        if self._ws_client:
            try:
                self._ws_client.stop()
            except Exception as e:
                logger.warning(f"Error stopping WebSocket client: {e}")
        logger.info("Feishu bot stopped")
    
    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> None:
        """Sync helper for adding reaction (runs in thread pool)."""
        try:
            request = CreateMessageReactionRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                ).build()
            
            response = self._client.im.v1.message_reaction.create(request)
            
            if not response.success():
                logger.warning(f"Failed to add reaction: code={response.code}, msg={response.msg}")
            else:
                logger.debug(f"Added {emoji_type} reaction to message {message_id}")
        except Exception as e:
            logger.warning(f"Error adding reaction: {e}")

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> None:
        """
        Add a reaction emoji to a message (non-blocking).
        
        Common emoji types: THUMBSUP, OK, EYES, DONE, OnIt, HEART
        """
        if not self._client or not Emoji:
            return
        
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)
    
    def _build_card_elements(self, content: str) -> list[dict[str, Any]]:
        """Build JSON 2.0-safe body elements for standard outbound messages."""
        text = str(content or "").strip()
        return [{"tag": "markdown", "content": text or "(empty)"}]

    @staticmethod
    def _legacy_table_to_markdown(table: dict[str, Any]) -> str | None:
        """Best-effort conversion from legacy table payload to markdown."""
        columns = table.get("columns")
        rows = table.get("rows")
        if not isinstance(columns, list) or not columns:
            return None
        if not isinstance(rows, list) or not rows:
            return None

        keys: list[str] = []
        headers: list[str] = []
        for idx, col in enumerate(columns):
            if not isinstance(col, dict):
                key = f"c{idx}"
                label = key
            else:
                key = str(col.get("name") or f"c{idx}")
                label = str(col.get("display_name") or key)
            keys.append(key)
            headers.append(label)

        lines = [
            f"| {' | '.join(headers)} |",
            f"| {' | '.join(['---'] * len(headers))} |",
        ]
        for row in rows:
            if not isinstance(row, dict):
                continue
            vals = [str(row.get(k) or "").replace("\n", "<br/>") for k in keys]
            lines.append(f"| {' | '.join(vals)} |")
        return "\n".join(lines) if len(lines) > 2 else None

    @staticmethod
    def _legacy_button_to_v2(button: dict[str, Any]) -> dict[str, Any] | None:
        """Convert legacy action.button payload to JSON 2.0 button format."""
        if not isinstance(button, dict):
            return None
        value = button.get("value")
        if not isinstance(value, dict):
            return None

        text = button.get("text")
        if not isinstance(text, dict):
            text = {"tag": "plain_text", "content": str(text or "Action")}

        out: dict[str, Any] = {
            "tag": "button",
            "text": text,
            "behaviors": [{"type": "callback", "value": value}],
        }
        btn_type = button.get("type")
        if isinstance(btn_type, str) and btn_type:
            out["type"] = btn_type
        confirm = button.get("confirm")
        if isinstance(confirm, dict):
            out["confirm"] = confirm
        return out

    def _upgrade_legacy_elements(self, elements: list[Any]) -> list[dict[str, Any]]:
        """Upgrade commonly used legacy elements to JSON 2.0-safe equivalents."""
        upgraded: list[dict[str, Any]] = []
        for element in elements:
            if not isinstance(element, dict):
                continue
            tag = str(element.get("tag") or "").strip().lower()

            if tag == "action":
                actions = element.get("actions")
                if not isinstance(actions, list):
                    continue
                buttons = [
                    button
                    for action in actions
                    if isinstance(action, dict)
                    for button in [self._legacy_button_to_v2(action)]
                    if button is not None
                ]
                if not buttons:
                    continue
                if len(buttons) == 1:
                    upgraded.append(buttons[0])
                    continue
                upgraded.append(
                    {
                        "tag": "column_set",
                        "horizontal_spacing": "8px",
                        "columns": [
                            {
                                "tag": "column",
                                "width": "weighted",
                                "weight": 1,
                                "elements": [button],
                            }
                            for button in buttons
                        ],
                    }
                )
                continue

            if tag == "div":
                text = element.get("text")
                if isinstance(text, dict):
                    content = str(text.get("content") or "").strip()
                else:
                    content = str(element.get("content") or "").strip()
                if content:
                    upgraded.append({"tag": "markdown", "content": content})
                continue

            if tag == "table":
                markdown = self._legacy_table_to_markdown(element)
                if markdown:
                    upgraded.append({"tag": "markdown", "content": markdown})
                continue

            upgraded.append(element)

        return upgraded

    def _normalize_card_payload(self, card: dict[str, Any]) -> dict[str, Any]:
        """Normalize card payload to JSON 2.0 structure."""
        if not isinstance(card, dict):
            return {
                "schema": "2.0",
                "config": {"width_mode": "fill", "update_multi": True},
                "body": {"elements": [{"tag": "markdown", "content": str(card)}]},
            }

        payload = dict(card)
        if "body" not in payload and isinstance(payload.get("elements"), list):
            payload["body"] = {"elements": payload.get("elements", [])}

        body = payload.get("body")
        if not isinstance(body, dict):
            body = {}
        elements = body.get("elements")
        if isinstance(elements, list):
            body["elements"] = self._upgrade_legacy_elements(elements)
        else:
            body["elements"] = []
        if not body["elements"]:
            body["elements"] = [{"tag": "markdown", "content": "(empty)"}]
        payload["body"] = body

        config = payload.get("config")
        if not isinstance(config, dict):
            config = {}
        if "width_mode" not in config and "wide_screen_mode" in config:
            config["width_mode"] = "fill" if bool(config.get("wide_screen_mode")) else "compact"
        config.pop("wide_screen_mode", None)
        config.setdefault("update_multi", True)
        payload["config"] = config

        payload["schema"] = "2.0"
        payload.pop("elements", None)
        payload.pop("i18n_elements", None)
        payload.pop("fallback", None)
        return payload

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Feishu."""
        if not self._client:
            logger.warning("Feishu client not initialized")
            return
        
        try:
            # Determine receive_id_type based on chat_id format
            # open_id starts with "ou_", chat_id starts with "oc_"
            if msg.chat_id.startswith("oc_"):
                receive_id_type = "chat_id"
            else:
                receive_id_type = "open_id"

            custom_card = msg.metadata.get("_feishu_card") if isinstance(msg.metadata, dict) else None
            if isinstance(custom_card, dict):
                card = self._normalize_card_payload(custom_card)
            else:
                # Build a simple markdown card to avoid unsupported legacy tags on JSON 2.0.
                elements = self._build_card_elements(msg.content)
                card = self._normalize_card_payload({
                    "config": {"width_mode": "fill", "update_multi": True},
                    "body": {"elements": elements},
                })
            content = json.dumps(card, ensure_ascii=False)
            
            request = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(msg.chat_id)
                    .msg_type("interactive")
                    .content(content)
                    .build()
                ).build()
            
            response = self._client.im.v1.message.create(request)
            
            if not response.success():
                logger.error(
                    f"Failed to send Feishu message: code={response.code}, "
                    f"msg={response.msg}, log_id={response.get_log_id()}"
                )
            else:
                logger.debug(f"Feishu message sent to {msg.chat_id}")
                
        except Exception as e:
            logger.error(f"Error sending Feishu message: {e}")
    
    def _on_message_sync(self, data: "P2ImMessageReceiveV1") -> None:
        """
        Sync handler for incoming messages (called from WebSocket thread).
        Schedules async handling in the main event loop.
        """
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)

    @staticmethod
    def _format_command_as_markdown(command: str) -> str:
        text = str(command or "").strip()
        if not text:
            return "`(not available)`"
        fence = "```"
        while fence in text:
            fence += "`"
        return f"{fence}bash\n{text}\n{fence}"

    @classmethod
    def _build_exec_approval_resolved_card(
        cls,
        approval_id: str,
        decision: str,
        *,
        command_preview: str = "",
        working_dir: str = "",
        risk_level: str = "",
    ) -> dict[str, Any]:
        """Build a static card to replace actionable approval buttons after click."""
        approved = decision == "allow-once"
        decision_text = "Allowed once" if approved else "Denied"
        decision_marker = "[ALLOW]" if approved else "[DENY]"
        card_template = "green" if approved else "red"
        risk_label = "hard-danger" if risk_level == "hard-danger" else "confirm"
        command_block = cls._format_command_as_markdown(command_preview)
        cwd_text = f"`{working_dir}`" if working_dir else "`(not available)`"
        return {
            "schema": "2.0",
            "config": {"width_mode": "fill", "update_multi": True},
            "header": {
                "template": card_template,
                "title": {"tag": "plain_text", "content": "Exec approval handled"},
            },
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": (
                            f"**ID:** `{approval_id}`\n"
                            f"**Decision:** {decision_marker} {decision_text}\n"
                            "**Command:**\n"
                            f"{command_block}\n"
                            f"**CWD:** {cwd_text}\n"
                            f"**Risk level:** `{risk_label}`\n"
                            "Decision submitted. This card is read-only now."
                        ),
                    }
                ]
            },
        }

    def _build_card_action_response(
        self,
        content: str,
        toast_type: str = "info",
        card_data: dict[str, Any] | None = None,
    ) -> Any:
        """Build callback response with toast and optional card replacement."""
        if P2CardActionTriggerResponse is None or CallBackToast is None:
            return None
        response = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = toast_type
        toast.content = content
        response.toast = toast
        if card_data and CallBackCard is not None:
            card = CallBackCard()
            card.type = "raw"
            card.data = card_data
            response.card = card
        return response

    def _on_card_action_sync(self, data: "P2CardActionTrigger") -> Any:
        """Sync card-action handler; convert clicks into /approve command messages."""
        event = getattr(data, "event", None)
        action = getattr(event, "action", None)
        value = getattr(action, "value", None)
        if not isinstance(value, dict) or str(value.get("type") or "").strip() != "exec_approval":
            logger.debug("Ignored unsupported Feishu card action payload")
            return self._build_card_action_response("Unsupported card action.", "error")

        approval_id = str(value.get("approval_id") or "").strip()
        decision = str(value.get("decision") or "").strip().lower()
        command_preview = str(value.get("command_preview") or "").strip()
        working_dir = str(value.get("working_dir") or "").strip()
        risk_level = str(value.get("risk_level") or "").strip().lower()
        if risk_level != "hard-danger":
            risk_level = "confirm"
        if not approval_id or decision not in {"allow-once", "deny"}:
            logger.warning("Invalid Feishu exec approval payload: {}", value)
            return self._build_card_action_response("Invalid approval action payload.", "error")

        operator = getattr(event, "operator", None)
        sender_id = str(
            getattr(operator, "open_id", None)
            or getattr(operator, "user_id", None)
            or ""
        ).strip()
        context = getattr(event, "context", None)
        chat_id = str(getattr(context, "open_chat_id", None) or "").strip() or sender_id
        open_message_id = str(getattr(context, "open_message_id", None) or "").strip()

        if not sender_id or not chat_id:
            logger.warning("Feishu card action missing sender/chat: sender_id={}, chat_id={}", sender_id, chat_id)
            return self._build_card_action_response("Missing sender or chat context.", "error")
        if self._loop and self._loop.is_running():
            logger.info(
                "Feishu card action received: approval_id={}, decision={}, sender_id={}, chat_id={}",
                approval_id,
                decision,
                sender_id,
                chat_id,
            )
            asyncio.run_coroutine_threadsafe(
                self._on_card_action(
                    sender_id=sender_id,
                    chat_id=chat_id,
                    approval_id=approval_id,
                    decision=decision,
                    open_message_id=open_message_id,
                ),
                self._loop,
            )
            return self._build_card_action_response(
                "Approval submitted.",
                "success",
                card_data=self._build_exec_approval_resolved_card(
                    approval_id,
                    decision,
                    command_preview=command_preview,
                    working_dir=working_dir,
                    risk_level=risk_level,
                ),
            )

        logger.warning("Feishu card action dropped: event loop unavailable")
        return self._build_card_action_response("Bot loop unavailable.", "error")

    async def _on_card_action(
        self,
        *,
        sender_id: str,
        chat_id: str,
        approval_id: str,
        decision: str,
        open_message_id: str,
    ) -> None:
        """Async card-action handling that reuses /approve fallback logic."""
        metadata: dict[str, Any] = {
            "msg_type": "interactive",
            "source": "card_action",
            "_suppress_progress": True,
            "approval_id": approval_id,
            "approval_decision": decision,
        }
        if open_message_id:
            metadata["message_id"] = open_message_id

        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=f"/approve {approval_id} {decision}",
            metadata=metadata,
        )

    async def _on_message(self, data: "P2ImMessageReceiveV1") -> None:
        """Handle incoming message from Feishu."""
        try:
            event = data.event
            message = event.message
            sender = event.sender
            
            # Deduplication check
            message_id = message.message_id
            if message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None
            
            # Trim cache: keep most recent 500 when exceeds 1000
            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)
            
            # Skip bot messages
            sender_type = sender.sender_type
            if sender_type == "bot":
                return
            
            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            chat_id = message.chat_id
            chat_type = message.chat_type  # "p2p" or "group"
            msg_type = message.message_type
            parent_id = (getattr(message, "parent_id", None) or "").strip()
            root_id = (getattr(message, "root_id", None) or "").strip()
            thread_id = (getattr(message, "thread_id", None) or "").strip()
            quoted_text: str | None = None
            quoted_msg_type: str | None = None
            
            # Add reaction to indicate "seen"
            await self._add_reaction(message_id, "THUMBSUP")
            
            # Parse message content
            raw_content = message.content or ""
            post_image_keys: list[str] = []
            if msg_type == "post":
                content, post_image_keys = self._extract_post_content(raw_content)
            else:
                content = self._parse_message_content(msg_type, raw_content)
            media_paths: list[str] = []

            if msg_type in {"image", "file", "audio"}:
                downloaded_path = await self._download_message_resource(
                    message_id=message_id,
                    msg_type=msg_type,
                    raw_content=raw_content,
                )
                if downloaded_path:
                    media_paths.append(downloaded_path)
                    content = self._merge_attachment_note(
                        content=content,
                        msg_type=msg_type,
                        note=f"[{msg_type}: {downloaded_path}]",
                    )
            elif msg_type == "post":
                for image_key in post_image_keys:
                    downloaded_path = await self._download_message_resource(
                        message_id=message_id,
                        msg_type="image",
                        raw_content=json.dumps({"image_key": image_key}, ensure_ascii=False),
                    )
                    if downloaded_path:
                        media_paths.append(downloaded_path)
                        content = self._merge_attachment_note(
                            content=content,
                            msg_type="image",
                            note=f"[post image: {downloaded_path}]",
                        )

            if parent_id and parent_id != message_id:
                quoted_text, quoted_msg_type = await self._fetch_quoted_message(parent_id)
                if quoted_text:
                    content = self._merge_quoted_message(content, quoted_text)
                elif not content:
                    content = f"[Quoted message: {parent_id}]"
            
            if not content and not media_paths:
                return
            
            # Forward to message bus
            reply_to = chat_id if chat_type == "group" else sender_id
            metadata: dict[str, Any] = {
                "message_id": message_id,
                "chat_type": chat_type,
                "msg_type": msg_type,
            }
            if parent_id:
                metadata["parent_id"] = parent_id
            if root_id:
                metadata["root_id"] = root_id
            if thread_id:
                metadata["thread_id"] = thread_id
            if quoted_msg_type:
                metadata["quoted_msg_type"] = quoted_msg_type

            await self._handle_message(
                sender_id=sender_id,
                chat_id=reply_to,
                content=content,
                media=media_paths,
                metadata=metadata,
            )
            
        except Exception as e:
            logger.error(f"Error processing Feishu message: {e}")

    def _parse_message_content(self, msg_type: str, raw_content: str) -> str:
        """Parse Feishu message payload into plain text for the agent."""
        if msg_type == "text":
            try:
                return json.loads(raw_content).get("text", "")
            except json.JSONDecodeError:
                return raw_content

        if msg_type == "post":
            return self._extract_post_text(raw_content)

        if msg_type == "interactive":
            return self._extract_interactive_text(raw_content)

        if msg_type == "merge_forward":
            return self._extract_merge_forward_label(raw_content)

        # Keep placeholders for other message types (image/file/audio/etc.)
        return MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]")

    async def _fetch_quoted_message(self, message_id: str) -> tuple[str | None, str | None]:
        """Fetch and parse quoted (parent) message content."""
        if not self._client or not FEISHU_AVAILABLE or GetMessageRequest is None:
            return None, None

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._fetch_quoted_message_sync, message_id)
        except Exception as e:
            logger.warning(f"Failed to fetch quoted Feishu message {message_id}: {e}")
            return None, None

    def _fetch_quoted_message_sync(self, message_id: str) -> tuple[str | None, str | None]:
        req = GetMessageRequest.builder().message_id(message_id).build()
        resp = self._client.im.v1.message.get(req)
        if not resp.success():
            code = getattr(resp, "code", None)
            msg = getattr(resp, "msg", None)
            log_id = None
            if hasattr(resp, "get_log_id") and callable(resp.get_log_id):
                try:
                    log_id = resp.get_log_id()
                except Exception:
                    log_id = None
            tail = f", log_id={log_id}" if log_id else ""
            raise RuntimeError(f"code={code}, msg={msg}{tail}")

        items = getattr(getattr(resp, "data", None), "items", None) or []
        if not items:
            return None, None

        parent_msg = items[0]
        parent_msg_type = (getattr(parent_msg, "msg_type", None) or "").strip()
        body = getattr(parent_msg, "body", None)
        raw_content = (getattr(body, "content", None) or "").strip()
        if not parent_msg_type:
            return None, None

        if parent_msg_type == "merge_forward":
            merged_preview = self._build_merge_forward_preview(items)
            if merged_preview:
                return self._truncate_quoted_preview(merged_preview), parent_msg_type

        preview = self._parse_message_content(parent_msg_type, raw_content).strip()
        if not preview:
            preview = MSG_TYPE_MAP.get(parent_msg_type, f"[{parent_msg_type}]")

        preview = self._truncate_quoted_preview(preview)
        return preview, parent_msg_type

    def _truncate_quoted_preview(self, text: str) -> str:
        cleaned = (text or "").strip()
        if len(cleaned) <= MAX_QUOTED_PREVIEW_CHARS:
            return cleaned
        return cleaned[:MAX_QUOTED_PREVIEW_CHARS].rstrip() + "... [truncated]"

    def _merge_quoted_message(self, content: str, quoted_text: str) -> str:
        quoted = (quoted_text or "").strip()
        if not quoted:
            return content

        quoted_lines = [f"> {line}" if line else ">" for line in quoted.splitlines()]
        quote_block = "[Quoted message]\n" + "\n".join(quoted_lines)

        current = (content or "").strip()
        if not current:
            return quote_block
        return f"{quote_block}\n\n{current}"

    def _merge_attachment_note(self, content: str, msg_type: str, note: str) -> str:
        """Replace placeholder-only content with a richer attachment note."""
        current = (content or "").strip()
        placeholder = MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]")
        if not current or current == placeholder:
            return note
        if note in current:
            return current
        return f"{current}\n{note}"

    def _extract_merge_forward_label(self, raw_content: str) -> str:
        """Extract a readable label from merge_forward message body."""
        text = (raw_content or "").strip()
        if text:
            return text
        return "[merge_forward]"

    def _extract_interactive_text(self, raw_content: str) -> str:
        """Extract a readable text preview from Feishu interactive card payload."""
        try:
            payload = json.loads(raw_content) if raw_content else {}
        except json.JSONDecodeError:
            return (raw_content or "").strip()

        def _walk(node: Any, out: list[str]) -> None:
            if isinstance(node, dict):
                tag = str(node.get("tag", "")).strip()
                if tag in {"text", "plain_text"}:
                    value = node.get("text")
                    if isinstance(value, str) and value.strip():
                        out.append(value.strip())
                elif tag in {"lark_md", "markdown"}:
                    value = node.get("content")
                    if isinstance(value, str) and value.strip():
                        out.append(value.strip())

                title = node.get("title")
                if isinstance(title, str) and title.strip():
                    out.append(title.strip())
                text = node.get("text")
                if isinstance(text, str) and text.strip():
                    out.append(text.strip())

                for child in node.values():
                    _walk(child, out)
                return

            if isinstance(node, list):
                for child in node:
                    _walk(child, out)
                return

        fragments: list[str] = []
        _walk(payload, fragments)

        merged_lines: list[str] = []
        seen: set[str] = set()
        for fragment in fragments:
            normalized = " ".join(fragment.split())
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged_lines.append(normalized)

        return "\n".join(merged_lines).strip()

    def _build_merge_forward_preview(self, items: list[Any]) -> str:
        """Build a compact text preview from merge_forward message item list."""
        previews: list[str] = []
        seen: set[str] = set()

        for item in items[1:]:
            msg_type = (getattr(item, "msg_type", None) or "").strip()
            body = getattr(item, "body", None)
            raw_content = (getattr(body, "content", None) or "").strip()
            if not msg_type:
                continue

            snippet = self._parse_message_content(msg_type, raw_content).strip()
            if not snippet:
                snippet = MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]")

            normalized = " ".join(snippet.split())
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            if len(normalized) > MAX_MERGE_FORWARD_LINE_CHARS:
                normalized = normalized[:MAX_MERGE_FORWARD_LINE_CHARS].rstrip() + "..."
            previews.append(normalized)

            if len(previews) >= MAX_MERGE_FORWARD_PREVIEW_ITEMS:
                break

        if previews:
            lines = ["[Merged forward history]"]
            lines.extend(f"{idx}. {line}" for idx, line in enumerate(previews, start=1))
            return "\n".join(lines)

        return ""

    async def _download_message_resource(
        self,
        message_id: str,
        msg_type: str,
        raw_content: str,
    ) -> str | None:
        """Download Feishu image/file message resource to local disk."""
        if not self._client or not FEISHU_AVAILABLE or GetMessageResourceRequest is None:
            return None

        resource_key, file_name_hint = self._extract_message_resource_ref(msg_type, raw_content)
        if not resource_key:
            return None

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None,
                self._download_message_resource_sync,
                message_id,
                msg_type,
                resource_key,
                file_name_hint,
            )
        except Exception as e:
            logger.warning(f"Failed to download Feishu {msg_type} resource for {message_id}: {e}")
            return None

    def _download_message_resource_sync(
        self,
        message_id: str,
        msg_type: str,
        resource_key: str,
        file_name_hint: str | None,
    ) -> str:
        # Feishu API only accepts image/file for message_resource type.
        request_type = "file" if msg_type == "audio" else msg_type
        req = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(resource_key)
            .type(request_type)
            .build()
        )
        resp = self._client.im.v1.message_resource.get(req)
        if not resp.success():
            code = getattr(resp, "code", None)
            msg = getattr(resp, "msg", None)
            log_id = None
            if hasattr(resp, "get_log_id") and callable(resp.get_log_id):
                try:
                    log_id = resp.get_log_id()
                except Exception:
                    log_id = None
            tail = f", log_id={log_id}" if log_id else ""
            raise RuntimeError(f"code={code}, msg={msg}{tail}")

        file_obj = getattr(resp, "file", None)
        if file_obj is None:
            raise RuntimeError("resource response missing file stream")

        if hasattr(file_obj, "getvalue"):
            data = file_obj.getvalue()
        else:
            data = file_obj.read()
        if not data:
            raise RuntimeError("resource response returned empty file")

        returned_name = getattr(resp, "file_name", None)
        output_path = self._build_inbound_media_path(
            message_id=message_id,
            msg_type=msg_type,
            file_name=(returned_name or file_name_hint or "").strip(),
        )
        output_path.write_bytes(data)
        logger.debug(f"Downloaded Feishu {msg_type} to {output_path}")
        return str(output_path)

    def _extract_message_resource_ref(self, msg_type: str, raw_content: str) -> tuple[str | None, str | None]:
        """Parse message content JSON and extract resource key + file name hint."""
        try:
            payload = json.loads(raw_content) if raw_content else {}
        except json.JSONDecodeError:
            return None, None
        if not isinstance(payload, dict):
            return None, None

        key_fields = {
            "image": ("image_key",),
            "file": ("file_key",),
            "audio": ("file_key",),
        }.get(msg_type, ())
        for key_field in key_fields:
            value = payload.get(key_field)
            if isinstance(value, str) and value.strip():
                file_name_hint = payload.get("file_name")
                if not isinstance(file_name_hint, str):
                    file_name_hint = None
                return value.strip(), file_name_hint
        return None, None

    def _build_inbound_media_path(self, message_id: str, msg_type: str, file_name: str) -> Path:
        """Create a stable local path for inbound Feishu attachments."""
        if self._workspace_dir:
            media_dir = self._workspace_dir / "downloads" / "feishu"
        else:
            media_dir = Path.home() / ".feibot" / "media" / "feishu"
        media_dir.mkdir(parents=True, exist_ok=True)

        cleaned_name = Path(file_name).name if file_name else ""
        suffix = Path(cleaned_name).suffix
        if not suffix:
            suffix = ".jpg" if msg_type == "image" else ""

        stem_src = Path(cleaned_name).stem if cleaned_name else msg_type
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem_src).strip("._")
        if not stem:
            stem = msg_type

        prefix = re.sub(r"[^A-Za-z0-9._-]+", "_", message_id)[:16] or "feishu"
        candidate = media_dir / f"{prefix}_{stem[:48]}{suffix}"
        if candidate.exists():
            candidate = media_dir / f"{prefix}_{stem[:40]}_{uuid.uuid4().hex[:8]}{suffix}"
        return candidate

    def _extract_post_text(self, raw_content: str) -> str:
        """Extract readable text from Feishu 'post' rich-text message."""
        text, _ = self._extract_post_content(raw_content)
        return text

    def _extract_post_content(self, raw_content: str) -> tuple[str, list[str]]:
        """Extract readable text and image keys from Feishu 'post' payload."""
        try:
            data = json.loads(raw_content)
        except json.JSONDecodeError:
            return "", []

        # Compatible with these shapes:
        # 1) {"post": {"zh_cn": {...}}}
        # 2) {"zh_cn": {...}}
        # 3) {"title": "...", "content": [...]}  # direct post payload
        post_root = data.get("post") if isinstance(data, dict) else None
        if not isinstance(post_root, dict):
            post_root = data if isinstance(data, dict) else {}

        def _extract_from_block(block: dict) -> tuple[str, list[str]]:
            if not isinstance(block, dict):
                return "", []

            parts: list[str] = []
            image_keys: list[str] = []
            title = (block.get("title") or "").strip()
            if title:
                parts.append(title)

            content_blocks = block.get("content")
            if isinstance(content_blocks, list):
                for line in content_blocks:
                    if not isinstance(line, list):
                        continue
                    line_parts: list[str] = []
                    for item in line:
                        if not isinstance(item, dict):
                            continue
                        tag = item.get("tag")
                        if tag == "text":
                            text = (item.get("text") or "").strip()
                            if text:
                                line_parts.append(text)
                        elif tag == "a":
                            text = (item.get("text") or item.get("href") or "").strip()
                            if text:
                                line_parts.append(text)
                        elif tag == "at":
                            name = (item.get("user_name") or item.get("name") or "@someone").strip()
                            line_parts.append(name)
                        elif tag == "img":
                            image_key = item.get("image_key")
                            if isinstance(image_key, str) and image_key.strip():
                                image_keys.append(image_key.strip())
                        elif tag in {"media", "emotion"}:
                            continue
                    if line_parts:
                        parts.append(" ".join(line_parts))

            text = "\n".join(p for p in parts if p).strip()
            return text, image_keys

        candidates: list[dict[str, Any]] = []
        if isinstance(post_root.get("content"), list):
            candidates.append(post_root)

        for key in ("zh_cn", "en_us", "ja_jp"):
            block = post_root.get(key)
            if isinstance(block, dict):
                candidates.append(block)

        for value in post_root.values():
            if isinstance(value, dict):
                candidates.append(value)

        seen_blocks: set[int] = set()
        for block in candidates:
            block_id = id(block)
            if block_id in seen_blocks:
                continue
            seen_blocks.add(block_id)
            text, image_keys = _extract_from_block(block)
            deduped_keys: list[str] = []
            seen_keys: set[str] = set()
            for key in image_keys:
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                deduped_keys.append(key)
            if text or deduped_keys:
                return text, deduped_keys

        return "", []
