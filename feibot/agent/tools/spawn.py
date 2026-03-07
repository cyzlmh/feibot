"""Spawn tool for background subagents or visible Feishu task chats."""

import json
import re
import uuid
from contextvars import ContextVar
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger

from feibot.agent.tools.base import Tool
from feibot.bus.events import InboundMessage

if TYPE_CHECKING:
    from feibot.agent.subagent import SubagentManager


class SpawnTool(Tool):
    """Spawn a background subagent or create a visible Feishu subtask chat."""

    def __init__(
        self,
        manager: "SubagentManager",
        feishu_app_id: str = "",
        feishu_app_secret: str = "",
        feishu_default_member_open_id: str = "",
        feishu_base_url: str = "https://open.feishu.cn",
    ):
        self._manager = manager
        self._origin_channel_ctx: ContextVar[str] = ContextVar("spawn_origin_channel", default="cli")
        self._origin_chat_id_ctx: ContextVar[str] = ContextVar("spawn_origin_chat_id", default="direct")
        self._origin_session_key_ctx: ContextVar[str] = ContextVar(
            "spawn_origin_session_key",
            default="cli:direct",
        )
        self._origin_sender_id_ctx: ContextVar[str] = ContextVar("spawn_origin_sender_id", default="")
        self._feishu_app_id = feishu_app_id
        self._feishu_app_secret = feishu_app_secret
        self._feishu_default_member_open_id = feishu_default_member_open_id
        self._feishu_base_url = feishu_base_url.rstrip("/")

    def set_context(
        self,
        channel: str,
        chat_id: str,
        sender_id: str | None = None,
        session_key: str | None = None,
    ) -> None:
        self._origin_channel_ctx.set(channel)
        self._origin_chat_id_ctx.set(chat_id)
        self._origin_session_key_ctx.set(session_key or f"{channel}:{chat_id}")
        self._origin_sender_id_ctx.set((sender_id or "").strip())

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        base = (
            "Start a separate task workspace. In Feishu chats, this creates a new Feishu group "
            "chat (user + bot) and runs the task there so the full trajectory is visible. In "
            "other channels, it uses a background subagent and summarizes back to the current chat."
        )
        policy = self._current_chat_policy_hint()
        return f"{base} {policy}" if policy else base

    def _current_chat_policy_hint(self) -> str:
        """Return a chat-specific policy hint for model tool selection."""
        origin_channel = self._origin_channel_ctx.get()
        origin_chat_id = self._origin_chat_id_ctx.get()
        if origin_channel != "feishu":
            return ""
        if origin_chat_id.startswith("ou_"):
            return (
                "Preferred policy for this chat: it is a Feishu direct chat (`ou_*`), "
                "so call this early for non-trivial tasks."
            )
        if origin_chat_id.startswith("oc_"):
            return (
                "Preferred policy for this chat: it is a Feishu group chat (`oc_*`), "
                "so do not auto-call unless the user explicitly asks."
            )
        return (
            "Preferred policy for this Feishu chat: if chat type is unclear, "
            "avoid auto-call unless the user explicitly asks."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task description for the background subagent."},
                "label": {"type": "string", "description": "Optional short label for progress display/chat naming."},
            },
            "required": ["task"],
            "additionalProperties": False,
        }

    async def execute(self, task: str, label: str | None = None, **kwargs: Any) -> str:
        origin_channel = self._origin_channel_ctx.get()
        origin_chat_id = self._origin_chat_id_ctx.get()
        origin_session_key = self._origin_session_key_ctx.get()
        if origin_channel == "feishu":
            return await self._spawn_feishu_task_chat(task=task, label=label)
        return await self._manager.spawn(
            task=task,
            label=label,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
            session_key=origin_session_key,
        )

    async def open_session(self, label: str | None = None) -> str:
        """Create a visible Feishu subtask chat without auto-running a task."""
        if self._origin_channel_ctx.get() != "feishu":
            return "Error: /sp is only supported in Feishu chats."
        return await self._open_feishu_chat(label=label, task=None)

    async def _spawn_feishu_task_chat(self, task: str, label: str | None = None) -> str:
        return await self._open_feishu_chat(label=label, task=task)

    async def _open_feishu_chat(self, label: str | None, task: str | None) -> str:
        origin_chat_id = self._origin_chat_id_ctx.get()
        if not self._feishu_app_id or not self._feishu_app_secret:
            return "Error: Feishu credentials not configured for spawn (channels.feishu.app_id/app_secret)."

        user_open_id = self._resolve_feishu_user_open_id()
        if not user_open_id:
            return (
                "Error: Cannot determine Feishu user open_id for spawn. "
                "Need current sender open_id or channels.feishu.allow_from[0]."
            )

        display_label = None
        task_text = (task or "").strip()
        if task_text:
            display_label = (label or task_text[:30]).strip() or "subtask"
            if label is None and len(task_text) > 30:
                display_label += "..."
            kickoff_text = (
                "子任务群已创建，后续处理将在此群进行。\n\n"
                f"任务：{task_text or display_label}"
            )
        else:
            kickoff_text = (
                "子任务群已创建，后续处理将在此群进行。\n\n"
                "请直接在本群描述任务，我会在这里继续处理。"
            )

        chat_name = self._build_feishu_chat_name(label=label, task=task_text or "subtask")

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                token = await self._feishu_get_tenant_token(client)
                chat = await self._feishu_create_chat(
                    client=client,
                    token=token,
                    owner_open_id=user_open_id,
                    name=chat_name,
                    description=f"Spawned from {origin_chat_id}",
                )
                chat_id = str(chat.get("chat_id") or "").strip()
                if not chat_id:
                    raise RuntimeError("create chat succeeded but chat_id missing")
                await self._feishu_add_members(client, token, chat_id, [user_open_id])
                await self._feishu_send_text(client, token, chat_id, kickoff_text)
        except Exception as e:
            return f"Error creating Feishu subtask chat: {e}"

        if task_text:
            await self._manager.bus.publish_inbound(
                InboundMessage(
                    channel="feishu",
                    sender_id=user_open_id,
                    chat_id=chat_id,
                    content=task_text,
                    timestamp=datetime.now(),
                    metadata={
                        "chat_type": "group",
                        "msg_type": "text",
                        "_spawn_bootstrap": True,
                        "_spawn_label": display_label,
                    },
                )
            )
            logger.info(
                "Spawn routed to Feishu subtask chat {} (origin={}, user={})",
                chat_id,
                origin_chat_id,
                user_open_id,
            )
            return (
                f"Created Feishu subtask chat `{chat_name}` ({chat_id}). "
                "Continue and review the full task trajectory there."
            )

        logger.info(
            "Opened Feishu subtask chat {} (origin={}, user={})",
            chat_id,
            origin_chat_id,
            user_open_id,
        )
        return (
            f"Created Feishu subtask chat `{chat_name}` ({chat_id}). "
            "Please continue in that chat."
        )

    def _resolve_feishu_user_open_id(self) -> str:
        sender = self._origin_sender_id_ctx.get().strip()
        if sender.startswith("ou_"):
            return sender
        fallback = self._feishu_default_member_open_id.strip()
        if fallback.startswith("ou_"):
            return fallback
        return ""

    def _build_feishu_chat_name(self, label: str | None, task: str) -> str:
        seed = (label or task or "subtask").strip()
        # Keep names short and ASCII-ish to avoid Feishu title-length surprises.
        slug = re.sub(r"[^A-Za-z0-9]+", "-", seed).strip("-").lower()
        if not slug:
            slug = "subtask"
        slug = slug[:18].strip("-") or "subtask"
        suffix = datetime.now().strftime("%H%M")
        name = f"feibot-{slug}-{suffix}"
        return name[:30]

    async def _feishu_get_tenant_token(self, client: httpx.AsyncClient) -> str:
        data = await self._feishu_request(
            client=client,
            method="POST",
            path="/open-apis/auth/v3/tenant_access_token/internal",
            json_body={"app_id": self._feishu_app_id, "app_secret": self._feishu_app_secret},
        )
        token = data.get("tenant_access_token")
        if not token:
            raise RuntimeError("tenant_access_token missing in response")
        return str(token)

    async def _feishu_create_chat(
        self,
        client: httpx.AsyncClient,
        token: str,
        owner_open_id: str,
        name: str,
        description: str,
    ) -> dict[str, Any]:
        req_uuid = uuid.uuid4().hex
        data = await self._feishu_request(
            client=client,
            method="POST",
            path=f"/open-apis/im/v1/chats?user_id_type=open_id&uuid={req_uuid}",
            token=token,
            json_body={
                "name": name,
                "description": description[:200],
                "owner_id": owner_open_id,
                "chat_mode": "group",
                "chat_type": "private",
                "external": False,
                "join_message_visibility": "all_members",
                "leave_message_visibility": "all_members",
                "membership_approval": "no_approval_required",
            },
        )
        return (data.get("data") or {}) if isinstance(data, dict) else {}

    async def _feishu_add_members(
        self,
        client: httpx.AsyncClient,
        token: str,
        chat_id: str,
        member_open_ids: list[str],
    ) -> None:
        await self._feishu_request(
            client=client,
            method="POST",
            path=f"/open-apis/im/v1/chats/{chat_id}/members?member_id_type=open_id&succeed_type=0",
            token=token,
            json_body={"id_list": member_open_ids},
        )

    async def _feishu_send_text(
        self,
        client: httpx.AsyncClient,
        token: str,
        chat_id: str,
        text: str,
    ) -> None:
        await self._feishu_request(
            client=client,
            method="POST",
            path="/open-apis/im/v1/messages?receive_id_type=chat_id",
            token=token,
            json_body={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )

    async def _feishu_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        token: str | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = await client.request(
            method=method,
            url=f"{self._feishu_base_url}{path}",
            json=json_body,
            headers=headers,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        if isinstance(data, dict) and data.get("code") == 0:
            return data
        code = data.get("code") if isinstance(data, dict) else None
        msg = data.get("msg") if isinstance(data, dict) else None
        log_id = data.get("log_id") if isinstance(data, dict) else None
        tail = f", log_id={log_id}" if log_id else ""
        raise RuntimeError(f"Feishu API {method} {path} failed: code={code}, msg={msg}{tail}")
