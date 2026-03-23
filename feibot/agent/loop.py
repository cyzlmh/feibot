"""Agent loop: the core processing engine."""

import asyncio
import copy
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from loguru import logger

from feibot.agent.context import ContextBuilder
from feibot.agent.exec_approval import (
    ApprovalDecision,
    ExecApprovalManager,
    ExecApprovalRequest,
    ExecApprovalResolution,
)
from feibot.agent.memory import MemoryStore
from feibot.agent.subagent import SubagentManager
from feibot.agent.tools.cron import CronTool
from feibot.agent.tools.feishu import FeishuSendFileTool
from feibot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from feibot.agent.tools.message import MessageTool
from feibot.agent.tools.registry import ToolRegistry
from feibot.agent.tools.search import FindFileTool, GrepTextTool
from feibot.agent.tools.shell import ExecTool
from feibot.agent.tools.web import WebFetchTool, WebSearchTool
from feibot.bus.events import InboundMessage, OutboundMessage
from feibot.bus.queue import MessageBus
from feibot.channels.allow_from import extract_allow_from_open_ids
from feibot.providers.base import LLMProvider, LLMResponse
from feibot.session.channel_log import ChannelLogStore, LogEntry
from feibot.session.manager import Session, SessionManager

_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": (
                            "A paragraph (2-5 sentences) summarizing key events/decisions/topics. "
                            "Start with [YYYY-MM-DD HH:MM]. Include detail useful for grep search."
                        ),
                    },
                    "memory_update": {
                        "type": "string",
                        "description": (
                            "Full updated long-term memory as markdown. Include all existing facts "
                            "plus new ones. Return unchanged if nothing new."
                        ),
                    },
                },
                "required": ["history_entry", "memory_update"],
            },
        },
    }
]

class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    SESSION_TOOL_RESULT_MAX_CHARS = 2000
    MEMORY_TOOL_RESULT_MAX_CHARS = 300
    PENDING_FILES_METADATA_KEY = "pending_files"
    RESUME_STATE_METADATA_KEY = "resume_state"
    COMMANDS_HELP_TEXT = (
        "🐈 feibot commands:\n"
        "/new — Start a new conversation\n"
        "/go — Continue the previous unfinished task\n"
        "/stop — Stop the current task\n"
        "/help — Show available commands\n"
        "/chatid — Show your user/chat IDs\n"
        "/sp [label] — Open a Feishu subtask group chat"
    )

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 20,
        max_consecutive_tool_errors: int = 3,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        memory_window: int = 50,
        brave_api_key: str | None = None,
        skills_env: dict[str, str] | None = None,
        exec_config: Any | None = None,
        feishu_config: Any | None = None,
        cron_service: Any | None = None,

        restrict_to_workspace: bool = False,
        allowed_dirs: list[str] | None = None,
        session_manager: SessionManager | None = None,
        debug: bool = False,
        agent_name: str = "feibot",
        disabled_tools: list[str] | None = None,
        llm_timeout: float | None = None,
    ):
        from feibot.config.schema import ExecToolConfig

        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.max_consecutive_tool_errors = max_consecutive_tool_errors
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.memory_window = memory_window
        self.brave_api_key = brave_api_key
        self.skills_env = {
            str(k): str(v)
            for k, v in (skills_env or {}).items()
            if str(k).strip()
        }
        self.exec_config = exec_config or ExecToolConfig()
        self.feishu_config = feishu_config
        self._feishu_base_url = "https://open.feishu.cn"
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self.allowed_dirs = allowed_dirs
        self.debug = debug
        self.agent_name = agent_name
        self.llm_timeout = float(llm_timeout) if llm_timeout and llm_timeout > 0 else None
        self.exec_approvals = ExecApprovalManager(
            enabled=bool(getattr(self.exec_config, "approval_enabled", True)),
            approvers=list(getattr(self.exec_config, "approval_approvers", []) or []),
        )

        self.context = ContextBuilder(workspace)
        self.sessions = session_manager or SessionManager(workspace / "sessions")
        self.channel_logs = ChannelLogStore(workspace / "logs")
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            brave_api_key=brave_api_key,
            skills_env=self.skills_env,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
            agent_name=self.agent_name,
        )

        self._running = False
        self._active_tasks: dict[str, list[asyncio.Task[None]]] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._consolidating: set[str] = set()
        self._consolidation_tasks: set[asyncio.Task] = set()
        self._consolidation_locks: dict[str, asyncio.Lock] = {}
        self._approval_execution_tasks: set[asyncio.Task[None]] = set()
        self._feishu_default_member_open_id = ""
        if self.feishu_config and getattr(self.feishu_config, "allow_from", None):
            allow_from_ids = extract_allow_from_open_ids(list(getattr(self.feishu_config, "allow_from", []) or []))
            if allow_from_ids:
                self._feishu_default_member_open_id = allow_from_ids[0]
        self._register_default_tools()

    @staticmethod
    def _normalize_approval_risk_level(raw: Any) -> str:
        level = str(raw or "").strip().lower()
        if level in {"", "none", "dangerous", "confirm"}:
            return level
        return ""

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        # File tools (restrict to workspace if configured)
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        self.tools.register(ReadFileTool(allowed_dir=allowed_dir))
        self.tools.register(WriteFileTool(allowed_dir=allowed_dir))
        self.tools.register(EditFileTool(allowed_dir=allowed_dir))
        self.tools.register(ListDirTool(allowed_dir=allowed_dir))
        self.tools.register(FindFileTool(base_dir=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(GrepTextTool(base_dir=self.workspace, allowed_dir=allowed_dir))

        # Shell tool
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.restrict_to_workspace,
            allowed_dirs=self.allowed_dirs,
            path_append=self.exec_config.path_append,
            injected_env=self.skills_env,
            approval_manager=self.exec_approvals,
            approval_workflow_resolver=lambda risk_level, channel, _sender_id: self._approval_workflow(
                channel,
                risk_level=risk_level,
            ),
        ))

        # Web tools
        self.tools.register(WebSearchTool(api_key=self.brave_api_key))
        self.tools.register(WebFetchTool())
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

        fs_cfg = self.feishu_config

        # Message tool
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))

        # Feishu file tool
        self.tools.register(
            FeishuSendFileTool(
                app_id=getattr(fs_cfg, "app_id", "") if fs_cfg else "",
                app_secret=getattr(fs_cfg, "app_secret", "") if fs_cfg else "",
                default_receive_id=self._feishu_default_member_open_id,
                default_receive_id_type="open_id",
                allowed_dir=allowed_dir,
            )
        )

    def _set_tool_context(
        self,
        channel: str,
        chat_id: str,
        sender_id: str = "",
        session_key: str | None = None,
    ) -> None:
        """Update per-request routing context for tools that send messages."""
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(channel, chat_id)
        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(channel, chat_id)
        exec_tool = self.tools.get("exec")
        if isinstance(exec_tool, ExecTool):
            exec_tool.set_context(
                channel=channel,
                chat_id=chat_id,
                sender_id=sender_id,
                session_key=session_key or f"{channel}:{chat_id}",
            )
        # Feishu file tool needs chat_id context for correct receive_id_type
        feishu_file_tool = self.tools.get("feishu_send_file")
        if isinstance(feishu_file_tool, FeishuSendFileTool):
            feishu_file_tool.set_context(chat_id)

    async def _open_sp_chat(
        self,
        *,
        label: str | None,
        origin_chat_id: str,
        sender_id: str,
        channel: str,
        source_session: Session,
    ) -> str:
        """Fork the current Feishu session into a new subtask group chat."""
        if channel != "feishu":
            return "Error: /sp is only supported in Feishu chats."
        app_id = str(getattr(self.feishu_config, "app_id", "") if self.feishu_config else "").strip()
        app_secret = str(getattr(self.feishu_config, "app_secret", "") if self.feishu_config else "").strip()
        if not app_id or not app_secret:
            return "Error: Feishu credentials not configured for /sp (channels.feishu.app_id/app_secret)."

        user_open_id = self._resolve_sp_user_open_id(sender_id)
        if not user_open_id:
            return (
                "Error: Cannot determine Feishu user open_id for /sp. "
                "Need current sender open_id or channels.feishu.allow_from[0]."
            )

        chat_name = self._build_sp_chat_name(label=label)
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                token = await self._feishu_get_tenant_token(client, app_id=app_id, app_secret=app_secret)
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
        except Exception as e:
            return f"Error creating Feishu subtask chat: {e}"

        self._fork_session_context(
            source_session=source_session,
            target_session_key=f"feishu:{chat_id}",
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

    def _fork_session_context(self, *, source_session: Session, target_session_key: str) -> None:
        """Clone full context (history + metadata) into the new chat session."""
        target = self.sessions.rotate(target_session_key)
        target.messages = copy.deepcopy(source_session.messages)
        target.metadata = copy.deepcopy(source_session.metadata)
        target.updated_at = datetime.now()
        target._saved_message_count = 0
        target._saved_state_fingerprint = ""
        self.sessions.save(target)

    def _resolve_sp_user_open_id(self, sender_id: str) -> str:
        sender = str(sender_id or "").strip()
        if sender.startswith("ou_"):
            return sender
        fallback = str(self._feishu_default_member_open_id or "").strip()
        if fallback.startswith("ou_"):
            return fallback
        return ""

    def _build_sp_chat_name(self, label: str | None) -> str:
        seed = (label or "subtask").strip()
        slug = re.sub(r"[^A-Za-z0-9]+", "-", seed).strip("-").lower()
        if not slug:
            slug = "subtask"
        slug = slug[:18].strip("-") or "subtask"
        suffix = datetime.now().strftime("%H%M")
        name = f"{self.agent_name}-{slug}-{suffix}"
        return name[:30]

    async def _feishu_get_tenant_token(self, client: httpx.AsyncClient, *, app_id: str, app_secret: str) -> str:
        data = await self._feishu_request(
            client=client,
            method="POST",
            path="/open-apis/auth/v3/tenant_access_token/internal",
            json_body={"app_id": app_id, "app_secret": app_secret},
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

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks some models place in visible content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    @staticmethod
    def _tokenize_command(content: str | None) -> list[str]:
        """
        Tokenize potential slash commands after stripping mention-like prefixes.

        Feishu group mentions often produce content like "@_user_1 /new". We
        strip leading mention-like tokens before matching commands.
        """
        raw = (content or "").strip()
        if not raw:
            return []

        tokens = [tok for tok in re.split(r"\s+", raw) if tok]
        while tokens:
            head = tokens[0].strip(",:，：")
            if head.startswith("@") or head.startswith("<at"):
                tokens.pop(0)
                continue
            break
        if not tokens:
            return []
        return [tok.strip(",:，：") for tok in tokens]

    @classmethod
    def _normalize_command(cls, content: str | None) -> str:
        """Return normalized command token (lowercase), if any."""
        tokens = cls._tokenize_command(content)
        if not tokens:
            return ""
        return tokens[0].lower()

    @classmethod
    def _parse_command(cls, content: str | None) -> tuple[str, str]:
        """Return (normalized command, remaining text)."""
        tokens = cls._tokenize_command(content)
        if not tokens:
            return "", ""
        cmd = tokens[0].lower()
        args = " ".join(tokens[1:]).strip()
        return cmd, args

    @staticmethod
    def _parse_approve_args(args: str) -> tuple[str | None, ApprovalDecision | None]:
        tokens = [tok for tok in re.split(r"\s+", args.strip()) if tok]
        if len(tokens) < 2:
            return None, None

        first_decision = ExecApprovalManager.normalize_decision(tokens[0])
        second_decision = ExecApprovalManager.normalize_decision(tokens[1])
        if first_decision:
            approval_id = " ".join(tokens[1:]).strip()
            return (approval_id or None), first_decision
        if second_decision:
            return tokens[0], second_decision
        return None, None

    @staticmethod
    def _format_command_as_markdown(command: str) -> str:
        text = str(command or "").strip()
        if not text:
            return "`(empty)`"
        fence = "```"
        while fence in text:
            fence += "`"
        return f"{fence}bash\n{text}\n{fence}"

    @staticmethod
    def _build_command_preview(command: str, *, max_chars: int = 320) -> str:
        """Compact one-line preview carried in card callback payloads."""
        text = " ".join(str(command or "").split()).strip()
        if not text:
            return "(empty)"
        if len(text) <= max_chars:
            return text
        return f"{text[: max_chars - 3]}..."

    def _approval_workflow(
        self,
        channel: str,
        *,
        risk_level: str = "confirm",
    ) -> str:
        if not bool(getattr(self.exec_config, "approval_enabled", True)):
            return "none"

        configured_level = self._normalize_approval_risk_level(
            getattr(self.exec_config, "approval_risk_level", "")
        )
        requires_approval = (
            configured_level == "confirm"
            or (configured_level == "dangerous" and risk_level == "dangerous")
        )

        if not requires_approval:
            return "none"
        if channel != "feishu":
            return "unavailable"
        return "feishu_card"

    def _build_exec_approval_card(self, request: ExecApprovalRequest) -> dict[str, Any]:
        command_block = self._format_command_as_markdown(request.command)
        command_preview = self._build_command_preview(request.command)
        risk_label = "dangerous" if str(request.risk_level).strip().lower() == "dangerous" else "confirm"
        callback_value_base = {
            "type": "exec_approval",
            "approval_id": request.id,
            "command_preview": command_preview,
            "working_dir": request.working_dir,
            "risk_level": risk_label,
        }
        return {
            "schema": "2.0",
            "config": {"width_mode": "fill", "update_multi": True},
            "header": {
                "template": "orange",
                "title": {"tag": "plain_text", "content": "Exec approval required"},
            },
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": (
                            f"**ID:** `{request.id}`\n"
                            "**About to run this shell command:**\n"
                            f"{command_block}\n"
                            f"**CWD:** `{request.working_dir}`\n"
                            f"**Risk level:** `{risk_label}`\n"
                            "This approval executes the command once."
                        ),
                    },
                    {
                        "tag": "column_set",
                        "horizontal_spacing": "8px",
                        "columns": [
                            {
                                "tag": "column",
                                "width": "weighted",
                                "weight": 1,
                                "elements": [
                                    {
                                        "tag": "button",
                                        "type": "primary",
                                        "text": {"tag": "plain_text", "content": "Allow Once"},
                                        "behaviors": [
                                            {
                                                "type": "callback",
                                                "value": {**callback_value_base, "decision": "allow-once"},
                                            }
                                        ],
                                    }
                                ],
                            },
                            {
                                "tag": "column",
                                "width": "weighted",
                                "weight": 1,
                                "elements": [
                                    {
                                        "tag": "button",
                                        "type": "danger",
                                        "text": {"tag": "plain_text", "content": "Deny"},
                                        "behaviors": [
                                            {
                                                "type": "callback",
                                                "value": {**callback_value_base, "decision": "deny"},
                                            }
                                        ],
                                    }
                                ],
                            },
                        ],
                    },
                ]
            },
        }

    async def _publish_exec_approval_prompt(self, request: ExecApprovalRequest) -> None:
        card_enabled = self._uses_feishu_card_approval(request)
        if request.channel == "feishu" and card_enabled:
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=request.channel,
                    chat_id=request.chat_id,
                    content="",
                    metadata={
                        "_suppress_progress": True,
                        "_feishu_card": self._build_exec_approval_card(request),
                        "_exec_approval_id": request.id,
                    },
                )
            )
            return

    def _uses_feishu_card_approval(self, request: ExecApprovalRequest) -> bool:
        workflow = self._approval_workflow(
            request.channel,
            risk_level=str(request.risk_level or "confirm"),
        )
        return workflow == "feishu_card"

    @staticmethod
    def _replace_exec_approval_pending_result(
        history_messages: list[dict[str, Any]],
        approval_id: str,
        replacement: str,
    ) -> bool:
        """Replace `approval-pending:<id>` in the latest exec tool result message."""
        for item in reversed(history_messages):
            if item.get("role") != "tool":
                continue
            pending_id = ExecTool.parse_approval_pending_id(str(item.get("content") or ""))
            if pending_id != approval_id:
                continue
            item["content"] = replacement
            return True
        return False

    def _schedule_exec_after_approval(self, resolution: ExecApprovalResolution) -> None:
        task = asyncio.create_task(self._run_approved_exec(resolution))
        setattr(task, "_session_key", resolution.request.session_key)
        self._approval_execution_tasks.add(task)

        def _cleanup(t: asyncio.Task[None]) -> None:
            self._approval_execution_tasks.discard(t)

        task.add_done_callback(_cleanup)

    @staticmethod
    def _serialize_approval_request(request: ExecApprovalRequest) -> dict[str, Any]:
        return {
            "id": request.id,
            "command": request.command,
            "working_dir": request.working_dir,
            "channel": request.channel,
            "chat_id": request.chat_id,
            "session_key": request.session_key,
            "requester_id": request.requester_id,
            "risk_level": request.risk_level,
            "created_at": request.created_at.isoformat(),
        }

    @staticmethod
    def _deserialize_approval_request(payload: Any) -> ExecApprovalRequest | None:
        if not isinstance(payload, dict):
            return None
        try:
            created_at = datetime.fromisoformat(str(payload.get("created_at") or ""))
        except Exception:
            return None
        try:
            return ExecApprovalRequest(
                id=str(payload.get("id") or "").strip(),
                command=str(payload.get("command") or ""),
                working_dir=str(payload.get("working_dir") or ""),
                channel=str(payload.get("channel") or ""),
                chat_id=str(payload.get("chat_id") or ""),
                session_key=str(payload.get("session_key") or ""),
                requester_id=str(payload.get("requester_id") or ""),
                risk_level=str(payload.get("risk_level") or "confirm") or "confirm",
                created_at=created_at,
            )
        except Exception:
            return None

    @staticmethod
    def _serialize_disabled_tools(disabled_tools: set[str] | None) -> list[str]:
        if not disabled_tools:
            return []
        return sorted(str(x).strip() for x in disabled_tools if str(x).strip())

    @staticmethod
    def _deserialize_disabled_tools(payload: Any) -> set[str] | None:
        if not isinstance(payload, list):
            return None
        restored = {str(x).strip() for x in payload if str(x).strip()}
        return restored or None

    def _build_resume_state(
        self,
        *,
        status: str,
        reason: str,
        user_goal: str,
        messages: list[dict[str, Any]],
        disabled_tools: set[str] | None,
        channel: str,
        chat_id: str,
        sender_id: str,
        metadata: dict[str, Any] | None,
        approval_request: ExecApprovalRequest | None = None,
        history_messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        state: dict[str, Any] = {
            "version": 1,
            "status": status,
            "reason": reason,
            "user_goal": user_goal,
            "messages": copy.deepcopy(messages),
            "disabled_tools": self._serialize_disabled_tools(disabled_tools),
            "channel": channel,
            "chat_id": chat_id,
            "sender_id": sender_id,
            "metadata": dict(metadata or {}),
            "updated_at": datetime.now().isoformat(),
        }
        if approval_request is not None:
            state["approval_request"] = self._serialize_approval_request(approval_request)
        if history_messages is not None:
            state["history_messages"] = copy.deepcopy(history_messages)
        return state

    @staticmethod
    def _get_resume_state(session: Session) -> dict[str, Any] | None:
        state = session.metadata.get(AgentLoop.RESUME_STATE_METADATA_KEY)
        return state if isinstance(state, dict) else None

    def _set_resume_state(self, session: Session, state: dict[str, Any] | None) -> None:
        if state:
            session.metadata[self.RESUME_STATE_METADATA_KEY] = state
        else:
            session.metadata.pop(self.RESUME_STATE_METADATA_KEY, None)

    @classmethod
    def _resume_state_matches_approval(cls, state: dict[str, Any] | None, approval_id: str) -> bool:
        if not isinstance(state, dict):
            return False
        request = state.get("approval_request")
        if not isinstance(request, dict):
            return False
        return str(request.get("id") or "").strip() == str(approval_id or "").strip()

    def _resume_request_from_state(self, state: dict[str, Any] | None) -> ExecApprovalRequest | None:
        if not isinstance(state, dict):
            return None
        return self._deserialize_approval_request(state.get("approval_request"))

    def _build_resume_messages_after_pause(
        self,
        *,
        messages: list[dict[str, Any]],
        remaining_tool_calls: list[Any],
        reason: str,
    ) -> list[dict[str, Any]]:
        """Build resume messages after a pause, adding error results for remaining tool calls."""
        resume_messages = copy.deepcopy(messages)
        for tool_call in remaining_tool_calls:
            tool_id = str(getattr(tool_call, "id", "") or "")
            tool_name = str(getattr(tool_call, "name", "") or "")
            if not tool_id or not tool_name:
                continue
            resume_messages = self.context.add_tool_result(
                resume_messages,
                tool_id,
                tool_name,
                f"Error: {reason}",
            )
        return resume_messages

    @staticmethod
    def _resume_messages_from_state(state: dict[str, Any] | None) -> list[dict[str, Any]] | None:
        if not isinstance(state, dict):
            return None
        messages = state.get("messages")
        if not isinstance(messages, list):
            return None
        return copy.deepcopy(messages)

    @staticmethod
    def _resume_history_messages_from_state(state: dict[str, Any] | None) -> list[dict[str, Any]] | None:
        if not isinstance(state, dict):
            return None
        history_messages = state.get("history_messages")
        if not isinstance(history_messages, list):
            return None
        return copy.deepcopy(history_messages)

    def _advance_resume_state_after_approval(
        self,
        state: dict[str, Any] | None,
        *,
        approval_id: str,
        replacement: str,
        status: str,
        reason: str,
    ) -> dict[str, Any] | None:
        if not isinstance(state, dict):
            return None
        messages = self._resume_messages_from_state(state)
        if messages is None:
            return None
        if not self._replace_exec_approval_pending_result(messages, approval_id, replacement):
            return None
        next_state = copy.deepcopy(state)
        next_state["messages"] = messages
        history_messages = self._resume_history_messages_from_state(state)
        if history_messages is not None:
            self._replace_exec_approval_pending_result(history_messages, approval_id, replacement)
            next_state["history_messages"] = history_messages
        next_state["status"] = status
        next_state["reason"] = reason
        next_state["updated_at"] = datetime.now().isoformat()
        next_state.pop("approval_request", None)
        return next_state

    async def _record_exec_deny_history(
        self,
        resolution: ExecApprovalResolution,
        *,
        denial_content: str,
        session_locked: bool = False,
    ) -> None:
        async def _write_denial() -> None:
            request = resolution.request
            session = self.sessions.get_or_create(request.session_key)
            denied_result = f"Error: exec approval denied (ID: {request.id})."
            resume_state = self._get_resume_state(session)
            next_state = self._advance_resume_state_after_approval(
                resume_state,
                approval_id=request.id,
                replacement=denied_result,
                status="denied",
                reason="exec_approval_denied",
            )
            if next_state is not None:
                history_messages = self._resume_history_messages_from_state(next_state)
                if history_messages:
                    self._append_session_history(session, history_messages)
            # Deny should end the pending flow and allow fresh user instructions.
            self._set_resume_state(session, None)

            session.add_message("assistant", denial_content, tools_used=["exec"])
            self.channel_logs.append(
                session,
                LogEntry(
                    role="assistant",
                    content=denial_content,
                    timestamp=datetime.now().isoformat(),
                    channel=request.channel,
                    chat_id=request.chat_id,
                ),
            )
            self.sessions.save(session)

        request = resolution.request
        if session_locked:
            await _write_denial()
            return

        lock = self._get_session_lock(request.session_key)
        try:
            async with lock:
                await _write_denial()
        finally:
            self._prune_session_lock(request.session_key, lock)

    @staticmethod
    async def _cancellation_checkpoint() -> None:
        """Yield control so pending task cancellation is raised quickly."""
        await asyncio.sleep(0)

    async def _chat_with_optional_timeout(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        chat_coro = self.provider.chat(
            messages=messages,
            tools=tools,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if self.llm_timeout is None:
            return await chat_coro
        try:
            return await asyncio.wait_for(chat_coro, timeout=self.llm_timeout)
        except asyncio.TimeoutError:
            timeout_str = f"{self.llm_timeout:g}"
            return LLMResponse(
                content=f"Error calling LLM: request timed out after {timeout_str} seconds.",
                finish_reason="error",
            )

    def _restore_resume_context(
        self,
        state: dict[str, Any],
        *,
        fallback_channel: str,
        fallback_chat_id: str,
        fallback_sender_id: str,
        fallback_metadata: dict[str, Any] | None = None,
    ) -> tuple[
        list[dict[str, Any]],
        str,
        set[str] | None,
        str,
        str,
        str,
        dict[str, Any],
    ] | None:
        messages = self._resume_messages_from_state(state)
        if messages is None:
            return None
        metadata = state.get("metadata")
        return (
            messages,
            str(state.get("user_goal") or ""),
            self._deserialize_disabled_tools(state.get("disabled_tools")),
            str(state.get("channel") or fallback_channel),
            str(state.get("chat_id") or fallback_chat_id),
            str(state.get("sender_id") or fallback_sender_id),
            dict(metadata) if isinstance(metadata, dict) else dict(fallback_metadata or {}),
        )

    async def _run_approved_exec(self, resolution: ExecApprovalResolution) -> None:
        request = resolution.request
        lock = self._get_session_lock(request.session_key)
        try:
            async with lock:
                session = self.sessions.get_or_create(request.session_key)
                resume_state = self._get_resume_state(session)
                if not self._resume_state_matches_approval(resume_state, request.id):
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=request.channel,
                            chat_id=request.chat_id,
                            content=(
                                f"Error: resume state missing for exec approval {request.id}. "
                                "The command was not executed."
                            ),
                            metadata={"_suppress_progress": True, "_exec_approval_id": request.id},
                        )
                    )
                    return

                self._set_tool_context(
                    request.channel,
                    request.chat_id,
                    request.requester_id,
                    session_key=request.session_key,
                )
                exec_tool = self.tools.get("exec")
                if not isinstance(exec_tool, ExecTool):
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=request.channel,
                            chat_id=request.chat_id,
                            content=f"Error: exec tool unavailable for approval {request.id}.",
                            metadata={"_suppress_progress": True},
                        )
                    )
                    return

                result = await exec_tool.execute(
                    command=request.command,
                    working_dir=request.working_dir,
                    _approval_granted=True,
                )
                result_text = str(result or "(no output)")
                if len(result_text) > 9000:
                    result_text = result_text[:9000] + "\n... (truncated)"
                logger.info(
                    "Approved exec executed: id={}, decision={}, command={}, result_preview={}",
                    request.id,
                    resolution.decision,
                    request.command[:200],
                    result_text[:160].replace("\n", " "),
                )

                next_state = self._advance_resume_state_after_approval(
                    resume_state,
                    approval_id=request.id,
                    replacement=result_text,
                    status="approved_ready",
                    reason="exec_approval_approved",
                )
                if next_state is None:
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=request.channel,
                            chat_id=request.chat_id,
                            content=(
                                f"Exec approval {self.exec_approvals.decision_label(resolution.decision)} "
                                f"(ID: {request.id}).\n\n"
                                f"Command: {self._format_command_as_markdown(request.command)}\n\n"
                                f"Result:\n{result_text}"
                            ),
                            metadata={"_suppress_progress": True, "_exec_approval_id": request.id},
                        )
                    )
                    return

                pre_resume_history = self._resume_history_messages_from_state(next_state)
                if pre_resume_history:
                    self._append_session_history(session, pre_resume_history)

                restored = self._restore_resume_context(
                    next_state,
                    fallback_channel=request.channel,
                    fallback_chat_id=request.chat_id,
                    fallback_sender_id=request.requester_id,
                    fallback_metadata={"_suppress_progress": True, "_exec_approval_id": request.id},
                )
                if restored is None:
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=request.channel,
                            chat_id=request.chat_id,
                            content=(
                                f"Error: restored exec approval state is incomplete for {request.id}. "
                                "The command ran, but the agent could not resume."
                            ),
                            metadata={"_suppress_progress": True, "_exec_approval_id": request.id},
                        )
                    )
                    return
                (
                    resume_messages,
                    resume_user_goal,
                    resume_disabled_tools,
                    resume_channel,
                    resume_chat_id,
                    resume_sender_id,
                    resume_metadata,
                ) = restored

                message_tool = self.tools.get("message")
                if isinstance(message_tool, MessageTool):
                    message_tool.start_turn()

                async def _resume_progress(content: str, *, tool_hint: bool = False) -> None:
                    if bool(resume_metadata.get("_suppress_progress")):
                        return
                    meta = dict(resume_metadata)
                    meta["_progress"] = True
                    meta["_tool_hint"] = tool_hint
                    meta["_exec_approval_id"] = request.id
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=resume_channel,
                        chat_id=resume_chat_id,
                        content=content,
                        metadata=meta,
                    ))

                async def _save_resume_checkpoint(checkpoint_messages: list[dict[str, Any]]) -> None:
                    state = self._build_resume_state(
                        status="running",
                        reason="checkpoint",
                        user_goal=resume_user_goal,
                        messages=checkpoint_messages,
                        disabled_tools=resume_disabled_tools,
                        channel=resume_channel,
                        chat_id=resume_chat_id,
                        sender_id=resume_sender_id,
                        metadata=resume_metadata,
                    )
                    self._set_resume_state(session, state)
                    self.sessions.save(session)

                final_content, tools_used, loop_meta = await self._run_agent_loop(
                    resume_messages,
                    user_goal=resume_user_goal,
                    on_progress=_resume_progress,
                    disabled_tools=resume_disabled_tools,
                    on_checkpoint=_save_resume_checkpoint,
                )

                stop_reason = str(loop_meta.get("stopped_reason") or "")
                if stop_reason == "exec_approval_pending":
                    next_pending_id = str(loop_meta.get("pending_approval_id") or "").strip()
                    if next_pending_id:
                        followup_request = self.exec_approvals.get_request(next_pending_id)
                        paused_messages = loop_meta.get("resume_messages")
                        paused_history = loop_meta.get("resume_base_history_messages")
                        if followup_request and isinstance(paused_messages, list):
                            state = self._build_resume_state(
                                status="awaiting_approval",
                                reason="exec_approval_pending",
                                user_goal=resume_user_goal,
                                messages=paused_messages,
                                disabled_tools=resume_disabled_tools,
                                channel=resume_channel,
                                chat_id=resume_chat_id,
                                sender_id=resume_sender_id,
                                metadata=resume_metadata,
                                approval_request=followup_request,
                                history_messages=(
                                    paused_history if isinstance(paused_history, list) else None
                                ),
                            )
                            self._set_resume_state(session, state)
                            await self._publish_exec_approval_prompt(followup_request)
                    self.sessions.save(session)
                    return

                if stop_reason in {"max_iterations", "loop_guard", "error_threshold", "llm_error", "empty_response"}:
                    paused_messages = loop_meta.get("resume_messages")
                    if isinstance(paused_messages, list):
                        state = self._build_resume_state(
                            status="paused",
                            reason=stop_reason,
                            user_goal=resume_user_goal,
                            messages=paused_messages,
                            disabled_tools=resume_disabled_tools,
                            channel=resume_channel,
                            chat_id=resume_chat_id,
                            sender_id=resume_sender_id,
                            metadata=resume_metadata,
                        )
                        self._set_resume_state(session, state)
                else:
                    self._set_resume_state(session, None)

                if final_content is None:
                    final_content = "I've completed processing but have no response to give."

                history_messages = loop_meta.get("history_messages") if isinstance(loop_meta, dict) else None
                if isinstance(history_messages, list) and history_messages:
                    self._append_session_history(session, history_messages)
                elif stop_reason not in {"llm_error", "empty_response"}:
                    session.add_message(
                        "assistant",
                        final_content,
                        tools_used=tools_used if tools_used else None,
                    )
                self.channel_logs.append(
                    session,
                    LogEntry(
                        role="assistant",
                        content=final_content,
                        timestamp=datetime.now().isoformat(),
                        channel=resume_channel,
                        chat_id=resume_chat_id,
                    ),
                )
                self.sessions.save(session)

                if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
                    return
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=resume_channel,
                        chat_id=resume_chat_id,
                        content=final_content,
                        metadata=resume_metadata,
                    )
                )
        finally:
            self._prune_session_lock(request.session_key, lock)

    @staticmethod
    def _filter_tool_definitions(
        tool_defs: list[dict[str, Any]],
        disabled_tools: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return tool defs with disabled tool names removed."""
        if not disabled_tools:
            return tool_defs
        blocked = {x.strip() for x in disabled_tools if isinstance(x, str) and x.strip()}
        if not blocked:
            return tool_defs
        out: list[dict[str, Any]] = []
        for td in tool_defs:
            fn = td.get("function") if isinstance(td, dict) else None
            name = fn.get("name") if isinstance(fn, dict) else None
            if isinstance(name, str) and name in blocked:
                continue
            out.append(td)
        return out

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hints for channel progress messages."""
        preferred_keys_by_tool: dict[str, list[str]] = {
            "message": ["content", "chat_id", "channel"],
            "cron": ["action", "message", "cron_expr", "every_seconds", "at", "job_id"],
            "exec": ["command"],
            "read_file": ["path"],
            "write_file": ["path"],
            "append_file": ["path"],
            "find_file": ["query", "root"],
            "grep_text": ["pattern", "root"],
            "list_dir": ["path"],
            "web_fetch": ["url"],
            "feishu_send_file": ["note", "file_path", "receive_id"],
        }
        default_preferred_keys = [
            "path",
            "query",
            "pattern",
            "command",
            "url",
            "content",
            "file_path",
            "root",
            "chat_id",
            "channel",
        ]

        hints: list[str] = []
        for tc in tool_calls:
            args = getattr(tc, "arguments", None) or {}
            preview_val: str | None = None
            if isinstance(args, dict) and args:
                for key in preferred_keys_by_tool.get(tc.name, default_preferred_keys):
                    val = args.get(key)
                    if isinstance(val, str) and val.strip():
                        preview_val = val
                        break
                if preview_val is None:
                    for val in args.values():
                        if isinstance(val, str) and val.strip():
                            preview_val = val
                            break

            if isinstance(preview_val, str):
                compact = re.sub(r"\s+", " ", preview_val).strip()
                hints.append(f"**{tc.name}**\n```\n{compact}\n```")
            else:
                hints.append(f"**{tc.name}**")
        return "\n\n".join(hints)

    @classmethod
    def _extract_file_refs_from_content(cls, content: str | None) -> list[str]:
        """Extract `[file: /path]` references from user-visible message text."""
        if not content:
            return []
        refs: list[str] = []
        for match in re.findall(r"\[file:\s*([^\]]+?)\]", content):
            path = str(match).strip()
            if path:
                refs.append(path)
        return refs

    @classmethod
    def _collect_inbound_file_refs(cls, msg: InboundMessage) -> list[str]:
        """Collect file paths from media payload and text annotations."""
        refs: list[str] = []
        seen: set[str] = set()

        for path in (msg.media or []):
            if not isinstance(path, str):
                continue
            p = path.strip()
            if not p or p in seen:
                continue
            seen.add(p)
            refs.append(p)

        for path in cls._extract_file_refs_from_content(msg.content):
            if path in seen:
                continue
            seen.add(path)
            refs.append(path)

        return refs

    @classmethod
    def _cache_pending_files(cls, session: Session, file_refs: list[str]) -> list[str]:
        """Append pending file refs to session metadata (deduped, bounded)."""
        if not file_refs:
            return []
        key = cls.PENDING_FILES_METADATA_KEY
        existing_raw = session.metadata.get(key)
        existing = existing_raw if isinstance(existing_raw, list) else []
        out: list[str] = []
        seen: set[str] = set()
        for item in existing + file_refs:
            if not isinstance(item, str):
                continue
            val = item.strip()
            if not val or val in seen:
                continue
            seen.add(val)
            out.append(val)
        if len(out) > 20:
            out = out[-20:]
        session.metadata[key] = out
        return out

    @classmethod
    def _pop_pending_files(cls, session: Session) -> list[str]:
        """Pop pending file refs from session metadata."""
        raw = session.metadata.pop(cls.PENDING_FILES_METADATA_KEY, None)
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out

    @staticmethod
    def _append_file_refs_to_goal(text: str, file_refs: list[str]) -> str:
        """Append cached file references to a user goal for the current turn."""
        if not file_refs:
            return text
        file_notes = "\n".join(f"[file: {p}]" for p in file_refs)
        if not text:
            return file_notes
        return f"{text}\n\n{file_notes}"

    def _build_incomplete_response(
        self,
        reason: str,
        user_goal: str,
        tools_used: list[str],
        recent_observations: list[str],
    ) -> str:
        """Build a user-facing summary when the loop stops before completion."""
        done = ", ".join(tools_used[-8:]) if tools_used else "none"
        obs = "\n".join(f"- {x}" for x in recent_observations[-5:]) if recent_observations else "- none"
        return (
            "任务未完全完成，已停止自动调用以避免无效循环。\n\n"
            f"停止原因: {reason}\n"
            f"原始目标: {user_goal}\n"
            f"已执行工具: {done}\n"
            "最近观察:\n"
            f"{obs}\n\n"
            "如果你希望继续，请发送 `/go`。我会从保存的暂停状态继续。"
        )

    def _is_tool_error_result(self, result: str | None) -> bool:
        """Heuristic to detect failed tool executions across diverse tool wrappers."""
        if result is None:
            return False
        text = result.strip().lower()
        if not text:
            return False
        error_prefixes = (
            "error",
            "failed",
            "exception",
            "traceback",
        )
        return text.startswith(error_prefixes)

    def _compact_tool_content(self, content: str, *, max_chars: int) -> str:
        """Bound tool result size when persisting into session/memory flows."""
        if len(content) <= max_chars:
            return content
        return content[:max_chars].rstrip() + "\n\n... [truncated] ..."

    def _append_session_history(self, session: Session, history_messages: list[dict[str, Any]]) -> None:
        """Persist a list of LLM-history-compatible messages into the session store."""
        for item in history_messages:
            role = item.get("role")
            if not role:
                continue
            content = str(item.get("content", ""))
            if role == "assistant" and not content.strip() and not item.get("tool_calls"):
                continue
            extra = {k: v for k, v in item.items() if k not in {"role", "content", "timestamp"}}
            session.add_message(role, content, **extra)

    @staticmethod
    def _llm_response_log_fields(response: Any | None) -> dict[str, Any]:
        """Extract persistent per-turn LLM metadata for session/raw logs."""
        if response is None:
            return {}

        extra: dict[str, Any] = {}
        model = getattr(response, "model", None)
        if isinstance(model, str) and model.strip():
            extra["model"] = model

        provider_payload = getattr(response, "provider_payload", None)
        if isinstance(provider_payload, dict) and provider_payload:
            extra["provider_payload"] = copy.deepcopy(provider_payload)

        return extra

    def _record_debug_entry(
        self,
        debug_log: list[dict[str, Any]] | None,
        messages: list[dict],
        tools: list[dict] | None,
        response: Any,
        iteration: int = 0,
        source: str = "agent_loop",
    ) -> None:
        """Record a debug entry for a single LLM call."""
        if not self.debug or debug_log is None:
            return

        tool_calls = []
        if response.tool_calls:
            for tc in response.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.name,
                    "arguments": copy.deepcopy(tc.arguments),
                })

        log_fields = self._llm_response_log_fields(response)
        requested_model = self.model
        provider_payload = log_fields.get("provider_payload")
        if isinstance(provider_payload, dict):
            requested_model = str(provider_payload.get("requested_model") or requested_model)

        debug_log.append({
            "call_id": f"{source}:{iteration}:{len(debug_log) + 1}",
            "timestamp": datetime.now().isoformat(),
            "source": source,
            "iteration": iteration,
            "request": {
                "model": requested_model,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "message_count": len(messages),
                "tool_count": len(tools) if tools else 0,
                "messages": copy.deepcopy(messages),
                "tools": copy.deepcopy(tools) if tools else [],
            },
            "response": {
                "content": response.content,
                "tool_calls": tool_calls,
                "finish_reason": response.finish_reason,
                "usage": copy.deepcopy(response.usage),
                "reasoning_content": response.reasoning_content,
                "model": log_fields.get("model"),
                "provider_payload": provider_payload,
            },
        })

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        user_goal: str,
        debug_log: list[dict[str, Any]] | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        disabled_tools: set[str] | None = None,
        on_checkpoint: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
    ) -> tuple[str | None, list[str], dict[str, Any]]:
        """
        Run the agent iteration loop.

        Args:
            initial_messages: Starting messages for the LLM conversation.

        Returns:
            Tuple of (final_content, list_of_tools_used, loop_metadata).
        """
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []
        recent_observations: list[str] = []
        history_messages: list[dict[str, Any]] = []
        loop_meta: dict[str, Any] = {"stopped_reason": "unknown", "history_messages": history_messages}
        last_tool_signature = ""
        repeated_tool_calls = 0
        recent_signatures: list[str] = []
        consecutive_tool_errors = 0
        last_llm_response: dict[str, Any] | None = None

        def _build_loop_meta(stopped_reason: str, **extra: Any) -> dict[str, Any]:
            meta: dict[str, Any] = {
                "stopped_reason": stopped_reason,
                "history_messages": history_messages,
            }
            if last_llm_response:
                meta["last_llm_response"] = copy.deepcopy(last_llm_response)
            meta.update(extra)
            return meta

        while iteration < self.max_iterations:
            iteration += 1

            if on_checkpoint:
                await on_checkpoint(copy.deepcopy(messages))

            tool_defs = self._filter_tool_definitions(
                self.tools.get_definitions(),
                disabled_tools=disabled_tools,
            )
            await self._cancellation_checkpoint()
            response = await self._chat_with_optional_timeout(
                messages=messages,
                tools=tool_defs,
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )

            self._record_debug_entry(debug_log, messages, tool_defs, response, iteration=iteration, source="main_loop")
            response_log_fields = self._llm_response_log_fields(response)
            if response_log_fields:
                last_llm_response = copy.deepcopy(response_log_fields)

            if response.has_tool_calls:
                if on_progress:
                    clean = self._strip_think(response.content)
                    if clean:
                        await on_progress(clean)
                    hint = self._tool_hint(response.tool_calls)
                    if hint:
                        await on_progress(hint, tool_hint=True)

                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments)
                        }
                    }
                    for tc in response.tool_calls
                ]
                assistant_turn: dict[str, Any] = {
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": copy.deepcopy(tool_call_dicts),
                }
                if response.reasoning_content:
                    assistant_turn["reasoning_content"] = response.reasoning_content
                assistant_turn.update(response_log_fields)
                history_messages.append(assistant_turn)

                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                )

                for tool_index, tool_call in enumerate(response.tool_calls):
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    signature = f"{tool_call.name}:{json.dumps(tool_call.arguments, sort_keys=True, ensure_ascii=False)}"
                    recent_signatures.append(signature)
                    if len(recent_signatures) > 8:
                        recent_signatures.pop(0)

                    if signature == last_tool_signature:
                        repeated_tool_calls += 1
                    else:
                        repeated_tool_calls = 0
                    last_tool_signature = signature

                    is_two_step_cycle = (
                        len(recent_signatures) >= 6
                        and recent_signatures[-6] == recent_signatures[-4] == recent_signatures[-2]
                        and recent_signatures[-5] == recent_signatures[-3] == recent_signatures[-1]
                        and recent_signatures[-6] != recent_signatures[-5]
                    )

                    if repeated_tool_calls >= 2 or is_two_step_cycle:
                        loop_reason = (
                            "detected repeated identical tool calls"
                            if repeated_tool_calls >= 2
                            else "detected cyclical two-step tool calls"
                        )
                        final_content = self._build_incomplete_response(
                            reason=loop_reason,
                            user_goal=user_goal,
                            tools_used=tools_used,
                            recent_observations=recent_observations,
                        )
                        history_messages.append({
                            "role": "assistant",
                            "content": final_content,
                            "tools_used": tools_used if tools_used else None,
                        })
                        resume_messages = self._build_resume_messages_after_pause(
                            messages=messages,
                            remaining_tool_calls=response.tool_calls[tool_index:],
                            reason="tool execution paused by loop guard before running the blocked call",
                        )
                        loop_meta = _build_loop_meta(
                            "loop_guard",
                            resume_messages=resume_messages,
                        )
                        return final_content, tools_used, loop_meta

                    await self._cancellation_checkpoint()
                    logger.info(f"Tool call: {tool_call.name}({args_str[:200]})")
                    if disabled_tools and tool_call.name in disabled_tools:
                        result = f"Error: Tool '{tool_call.name}' not found"
                    else:
                        result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    pending_approval_id = (
                        ExecTool.parse_approval_pending_id(result)
                        if tool_call.name == "exec"
                        else None
                    )
                    result_for_history = self._compact_tool_content(
                        result or "",
                        max_chars=self.SESSION_TOOL_RESULT_MAX_CHARS,
                    )
                    history_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": result_for_history,
                    })
                    result_preview = (result or "").strip().replace("\n", " ")
                    if len(result_preview) > 180:
                        result_preview = result_preview[:180] + "..."
                    recent_observations.append(f"{tool_call.name}: {result_preview}")

                    if self._is_tool_error_result(result):
                        consecutive_tool_errors += 1
                    else:
                        consecutive_tool_errors = 0

                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )

                    if pending_approval_id:
                        request = self.exec_approvals.get_request(pending_approval_id)
                        base_history_messages = copy.deepcopy(history_messages)
                        resume_messages = self._build_resume_messages_after_pause(
                            messages=messages,
                            remaining_tool_calls=response.tool_calls[tool_index + 1:],
                            reason="tool execution deferred because exec approval paused the loop",
                        )
                        card_only_mode = bool(request and self._uses_feishu_card_approval(request))
                        final_content = (
                            f"Exec approval required (ID: {request.id})."
                            if card_only_mode and request
                            else self.exec_approvals.describe_request_text(request)
                            if request
                            else (
                                "Exec approval required but request context was lost."
                            )
                        )
                        if not card_only_mode:
                            history_messages.append({
                                "role": "assistant",
                                "content": final_content,
                                "tools_used": tools_used if tools_used else None,
                            })
                        loop_meta = _build_loop_meta(
                            "exec_approval_pending",
                            pending_approval_id=pending_approval_id,
                            resume_messages=resume_messages,
                            resume_base_history_messages=base_history_messages,
                        )
                        return final_content, tools_used, loop_meta

                    if consecutive_tool_errors >= self.max_consecutive_tool_errors:
                        final_content = self._build_incomplete_response(
                            reason="too many consecutive tool errors",
                            user_goal=user_goal,
                            tools_used=tools_used,
                            recent_observations=recent_observations,
                        )
                        history_messages.append({
                            "role": "assistant",
                            "content": final_content,
                            "tools_used": tools_used if tools_used else None,
                        })
                        resume_messages = self._build_resume_messages_after_pause(
                            messages=messages,
                            remaining_tool_calls=response.tool_calls[tool_index + 1:],
                            reason="tool execution skipped because the loop paused after consecutive tool errors",
                        )
                        loop_meta = _build_loop_meta(
                            "error_threshold",
                            resume_messages=resume_messages,
                        )
                        return final_content, tools_used, loop_meta

                    if tool_call.name == "message":
                        message_tool = self.tools.get("message")
                        if isinstance(message_tool, MessageTool) and message_tool.finish_requested:
                            content_arg = tool_call.arguments.get("content")
                            final_content = str(content_arg).strip() if content_arg is not None else ""
                            loop_meta = _build_loop_meta("message_finish")
                            return final_content or "Message sent.", tools_used, loop_meta
            else:
                clean = self._strip_think(response.content)
                if response.finish_reason == "error":
                    logger.error("LLM returned error: {}", (clean or response.content or "")[:200])
                    final_content = clean or response.content or "Sorry, I encountered an error calling the AI model."
                    loop_meta = _build_loop_meta("llm_error", resume_messages=copy.deepcopy(messages))
                    break

                final_candidate = clean or response.content
                if not final_candidate:
                    final_content = "I've completed processing but have no response to give."
                    loop_meta = _build_loop_meta("empty_response", resume_messages=copy.deepcopy(messages))
                    break

                final_content = final_candidate
                assistant_final: dict[str, Any] = {
                    "role": "assistant",
                    "content": final_content,
                }
                if response.reasoning_content:
                    assistant_final["reasoning_content"] = response.reasoning_content
                if tools_used:
                    assistant_final["tools_used"] = tools_used
                assistant_final.update(response_log_fields)
                history_messages.append(assistant_final)
                loop_meta = _build_loop_meta("completed")
                break

        if final_content is None:
            final_content = self._build_incomplete_response(
                reason=f"reached max tool iterations ({self.max_iterations})",
                user_goal=user_goal,
                tools_used=tools_used,
                recent_observations=recent_observations,
            )
            history_messages.append({
                "role": "assistant",
                "content": final_content,
                "tools_used": tools_used if tools_used else None,
            })
            loop_meta = _build_loop_meta(
                "max_iterations",
                resume_messages=copy.deepcopy(messages),
            )

        if "history_messages" not in loop_meta:
            loop_meta["history_messages"] = history_messages
        return final_content, tools_used, loop_meta

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if self._normalize_command(msg.content) == "/stop":
                await self._handle_stop(msg)
            else:
                task = asyncio.create_task(self._dispatch(msg))
                self._active_tasks.setdefault(msg.session_key, []).append(task)
                task.add_done_callback(
                    lambda t, k=msg.session_key: (
                        self._active_tasks.get(k, []).remove(t)
                        if t in self._active_tasks.get(k, [])
                        else None
                    )
                )

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        approval_tasks = [
            task
            for task in self._approval_execution_tasks
            if str(getattr(task, "_session_key", "")) == msg.session_key
        ]
        wait_tasks: list[asyncio.Task[Any]] = list(dict.fromkeys([*tasks, *approval_tasks]))
        cancelled = sum(1 for t in wait_tasks if not t.done() and t.cancel())
        if wait_tasks:
            await asyncio.gather(*wait_tasks, return_exceptions=True)

        lock = self._session_locks.get(msg.session_key)
        if lock is not None and lock.locked():
            while lock.locked():
                await asyncio.sleep(0.01)
        if lock is not None and not lock.locked():
            self._prune_session_lock(msg.session_key, lock)

        sub_cancelled = await self.subagents.cancel_by_session(msg.session_key)
        total = cancelled + sub_cancelled
        content = f"⏹ Stopped {total} task(s)." if total else "No active task to stop."
        await self.bus.publish_outbound(
            OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content)
        )

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message under a per-session lock."""
        lock = self._get_session_lock(msg.session_key)
        async with lock:
            try:
                response = await self._process_message(msg)
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="",
                            metadata=msg.metadata or {},
                        )
                    )
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content="Sorry, I encountered an error.",
                    )
                )
            finally:
                self._prune_session_lock(msg.session_key, lock)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        for tasks in self._active_tasks.values():
            for task in tasks:
                if not task.done():
                    task.cancel()
        self._active_tasks.clear()
        for task in list(self._approval_execution_tasks):
            if not task.done():
                task.cancel()
        self._approval_execution_tasks.clear()
        logger.info("Agent loop stopping")

    def _get_session_lock(self, session_key: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_key] = lock
        return lock

    def _prune_session_lock(self, session_key: str, lock: asyncio.Lock) -> None:
        """Drop session lock entries when no longer in use."""
        if not lock.locked():
            self._session_locks.pop(session_key, None)

    def _get_consolidation_lock(self, session_key: str) -> asyncio.Lock:
        lock = self._consolidation_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._consolidation_locks[session_key] = lock
        return lock

    def _prune_consolidation_lock(self, session_key: str, lock: asyncio.Lock) -> None:
        """Drop lock entry when no longer in use."""
        if not lock.locked():
            self._consolidation_locks.pop(session_key, None)

    def _schedule_archive_all_consolidation(
        self,
        session_key: str,
        snapshot: list[dict[str, Any]],
    ) -> None:
        """Archive a /new snapshot in the background without blocking the queue."""
        if not snapshot:
            return

        temp = Session(key=session_key)
        temp.messages = list(snapshot)
        lock = self._get_consolidation_lock(session_key)
        self._consolidating.add(session_key)

        async def _archive_and_unlock() -> None:
            try:
                async with lock:
                    ok = await self._consolidate_memory(temp, archive_all=True)
                    if not ok:
                        logger.warning(
                            f"/new background archival skipped/failed for {session_key}; session already reset"
                        )
            except Exception as e:
                logger.error(f"/new background archival failed for {session_key}: {e}")
            finally:
                self._consolidating.discard(session_key)
                self._prune_consolidation_lock(session_key, lock)
                task = asyncio.current_task()
                if task is not None:
                    self._consolidation_tasks.discard(task)

        task = asyncio.create_task(_archive_and_unlock())
        self._consolidation_tasks.add(task)

    async def _process_message(self, msg: InboundMessage, session_key: str | None = None) -> OutboundMessage | None:
        """
        Process a single inbound message.

        Args:
            msg: The inbound message to process.
            session_key: Override session key (used by process_direct).

        Returns:
            The response message, or None if no response needed.
        """
        if msg.channel == "system":
            channel, chat_id = (
                msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
            )
            logger.info(f"Processing system message from {msg.sender_id} -> {channel}:{chat_id}")
            key = f"{channel}:{chat_id}"
            self._set_tool_context(channel, chat_id, msg.sender_id, session_key=key)
            message_tool = self.tools.get("message")
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

            session = self.sessions.get_or_create(key)
            debug_log: list[dict[str, Any]] | None = [] if self.debug else None
            initial_messages = self.context.build_messages(
                history=session.get_history(max_messages=self.memory_window),
                current_message=msg.content,
                channel=channel,
                chat_id=chat_id,
            )
            final_content, tools_used, loop_meta = await self._run_agent_loop(
                initial_messages,
                user_goal=msg.content,
                debug_log=debug_log,
                on_progress=None,
            )
            final_content = final_content or "Background task completed."
            stop_reason = loop_meta.get("stopped_reason") if isinstance(loop_meta, dict) else None

            history_messages = loop_meta.get("history_messages") if isinstance(loop_meta, dict) else None
            if isinstance(history_messages, list) and history_messages:
                self._append_session_history(session, history_messages)
            elif stop_reason not in {"llm_error", "empty_response"}:
                session.add_message(
                    "assistant",
                    final_content,
                    tools_used=tools_used if tools_used else None,
                )
            if self.debug and debug_log:
                session.metadata.setdefault("debug_log", []).extend(debug_log)
            self.sessions.save(session)

            if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
                return None
            return OutboundMessage(channel=channel, chat_id=chat_id, content=final_content, metadata={})

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info(f"Processing message from {msg.channel}:{msg.sender_id}: {preview}")
        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)
        self._set_tool_context(msg.channel, msg.chat_id, msg.sender_id, session_key=key)
        disabled_tools: set[str] | None = None
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.start_turn()

        msg_type = str((msg.metadata or {}).get("msg_type") or "")
        message_id = str(
            msg.metadata.get("message_id")
            or msg.metadata.get("event_id")
            or msg.metadata.get("update_id")
            or f"{msg.channel}:{msg.chat_id}:{msg.sender_id}:{msg.timestamp.isoformat()}"
        )

        # Handle slash commands
        cmd, cmd_args = self._parse_command(msg.content)
        resume_state = self._get_resume_state(session)
        if cmd == "/stop":
            sub_cancelled = await self.subagents.cancel_by_session(key)
            content = f"⏹ Stopped {sub_cancelled} task(s)." if sub_cancelled else "No active task to stop."
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
            )
        if cmd == "/approve":
            is_card_action = str((msg.metadata or {}).get("source") or "") == "card_action"
            if not is_card_action:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="❌ Text approval is disabled. Use the Feishu approval card.",
                )
            approval_id, decision = self._parse_approve_args(cmd_args)
            if not approval_id or not decision:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="Usage: /approve <id> allow-once|deny",
                )

            resolution, error = self.exec_approvals.resolve(
                approval_id=approval_id,
                decision=decision,
                resolved_by=msg.sender_id,
            )
            if resolution is None:
                resume_request = None
                if self._resume_state_matches_approval(resume_state, approval_id):
                    resume_request = self._resume_request_from_state(resume_state)
                if resume_request is not None:
                    if not self.exec_approvals._is_authorized_approver(resume_request, msg.sender_id):
                        error = "You are not authorized to resolve this approval request."
                    else:
                        resolution = ExecApprovalResolution(
                            request=resume_request,
                            decision=decision,
                            resolved_by=msg.sender_id,
                            resolved_at=datetime.now(),
                        )
                        error = ""
            if resolution is None:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"❌ {error}",
                )

            if resolution.decision == "deny":
                deny_content = f"✅ Exec approval denied (ID: {resolution.request.id})."
                current_task = asyncio.current_task()
                session_locked = bool(
                    current_task and current_task in self._active_tasks.get(key, [])
                )
                await self._record_exec_deny_history(
                    resolution,
                    denial_content=deny_content,
                    session_locked=session_locked,
                )
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=deny_content,
                )

            self._schedule_exec_after_approval(resolution)
            if is_card_action:
                return None
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=(
                    f"✅ Exec approval {self.exec_approvals.decision_label(resolution.decision)} "
                    f"(ID: {resolution.request.id})."
                ),
            )
        if cmd == "/new":
            self.sessions.rotate(session.key)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="New session started.",
            )
        if cmd == "/help":
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=self.COMMANDS_HELP_TEXT,
            )
        if cmd == "/chatid":
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=f"User ID: `{msg.sender_id}`\nChat ID: `{msg.chat_id}`",
            )
        if cmd == "/sp":
            spawn_result = await self._open_sp_chat(
                label=cmd_args or None,
                origin_chat_id=msg.chat_id,
                sender_id=msg.sender_id,
                channel=msg.channel,
                source_session=session,
            )
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=spawn_result,
            )

        effective_content = msg.content
        restored_initial_messages: list[dict[str, Any]] | None = None
        if cmd in {"/go", "继续", "continue"}:
            if not resume_state:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="No paused task to resume.",
                )
            state_status = str(resume_state.get("status") or "").strip()
            if state_status == "awaiting_approval":
                approval_request = self._resume_request_from_state(resume_state)
                approval_id = approval_request.id if approval_request else "unknown"
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"Exec approval still required (ID: {approval_id}). Approve or deny it before resuming.",
                    metadata=msg.metadata or {},
                )
            restored = self._restore_resume_context(
                resume_state,
                fallback_channel=msg.channel,
                fallback_chat_id=msg.chat_id,
                fallback_sender_id=msg.sender_id,
                fallback_metadata=msg.metadata if isinstance(msg.metadata, dict) else {},
            )
            if restored is None:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="Saved resume state is incomplete and cannot be resumed.",
                )
            (
                restored_initial_messages,
                effective_content,
                restored_disabled_tools,
                _resume_channel,
                _resume_chat_id,
                _resume_sender_id,
                _resume_metadata,
            ) = restored
            if restored_disabled_tools is not None:
                disabled_tools = set(restored_disabled_tools)

        # Record raw inbound message for audit/replay and backfill older unsynced messages.
        self.channel_logs.append(
            session,
            LogEntry(
                role="user",
                content=msg.content,
                timestamp=msg.timestamp.isoformat(),
                message_id=message_id,
                sender_id=msg.sender_id,
                channel=msg.channel,
                chat_id=msg.chat_id,
                metadata=msg.metadata or {},
            ),
        )
        synced_count = self.channel_logs.sync_users_to_session(
            key,
            session,
            exclude_message_id=message_id,
        )
        if synced_count > 0:
            logger.info(f"Backfilled {synced_count} user messages from raw log for session {key}")
            self.sessions.save(session)

        if msg_type in {"file", "image", "audio"}:
            file_refs = self._collect_inbound_file_refs(msg)
            cached_files = self._cache_pending_files(session, file_refs)
            type_names = {"file": "文件", "image": "图片", "audio": "音频"}
            type_name = type_names.get(msg_type, "文件")
            final_content = (
                f"已收到{type_name}并缓存。\n"
                "请再发一条文字说明你的意图（例如：总结、提取要点、翻译、生成笔记、OCR、描述图片）。"
            )
            if not cached_files:
                final_content += "\n\n（提示：当前消息里没有可用的本地文件路径，后续处理可能需要通道侧先下载附件。）"

            logger.info(f"Response to {msg.channel}:{msg.sender_id}: {final_content}")
            session.add_message(
                "user",
                msg.content,
                message_id=message_id,
                sender_id=msg.sender_id,
                channel=msg.channel,
                chat_id=msg.chat_id,
            )
            session.add_message("assistant", final_content)
            self.channel_logs.append(
                session,
                LogEntry(
                    role="assistant",
                    content=final_content,
                    timestamp=datetime.now().isoformat(),
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                ),
            )
            self.sessions.save(session)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=final_content,
                metadata=msg.metadata or {},
            )

        debug_log: list[dict[str, Any]] | None = [] if self.debug else None
        if msg_type in {"text", "post"} and cmd not in {
            "/go",
            "/new",
            "/stop",
            "/help",
            "/chatid",
            "/sp",
            "/approve",
        }:
            pending_files = self._pop_pending_files(session)
            effective_content = self._append_file_refs_to_goal(effective_content, pending_files)
        if restored_initial_messages is not None:
            initial_messages = restored_initial_messages
        else:
            initial_messages = self.context.build_messages(
                history=session.get_history(max_messages=self.memory_window),
                current_message=effective_content,
                media=msg.media if msg.media else None,
                channel=msg.channel,
                chat_id=msg.chat_id,
            )

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            if bool((msg.metadata or {}).get("_suppress_progress")):
                return
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
                metadata=meta,
            ))

        async def _save_resume_checkpoint(checkpoint_messages: list[dict[str, Any]]) -> None:
            state = self._build_resume_state(
                status="running",
                reason="checkpoint",
                user_goal=effective_content,
                messages=checkpoint_messages,
                disabled_tools=disabled_tools,
                channel=msg.channel,
                chat_id=msg.chat_id,
                sender_id=msg.sender_id,
                metadata=msg.metadata if isinstance(msg.metadata, dict) else {},
            )
            self._set_resume_state(session, state)
            self.sessions.save(session)

        final_content, tools_used, loop_meta = await self._run_agent_loop(
            initial_messages,
            user_goal=effective_content,
            debug_log=debug_log,
            on_progress=_bus_progress,
            disabled_tools=disabled_tools,
            on_checkpoint=_save_resume_checkpoint,
        )

        stop_reason = loop_meta.get("stopped_reason")
        suppress_outbound_for_pending = False
        if stop_reason == "exec_approval_pending":
            pending_id = str(loop_meta.get("pending_approval_id") or "").strip()
            if pending_id:
                request = self.exec_approvals.get_request(pending_id)
                resume_messages = loop_meta.get("resume_messages")
                resume_history = loop_meta.get("resume_base_history_messages")
                if request:
                    if isinstance(resume_messages, list):
                        state = self._build_resume_state(
                            status="awaiting_approval",
                            reason="exec_approval_pending",
                            user_goal=effective_content,
                            messages=resume_messages,
                            disabled_tools=disabled_tools,
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            sender_id=msg.sender_id,
                            metadata=msg.metadata if isinstance(msg.metadata, dict) else {},
                            approval_request=request,
                            history_messages=(
                                resume_history if isinstance(resume_history, list) else None
                            ),
                        )
                        self._set_resume_state(session, state)
                    await self._publish_exec_approval_prompt(request)
                    suppress_outbound_for_pending = self._uses_feishu_card_approval(request)
        else:
            resume_messages = loop_meta.get("resume_messages")
            if stop_reason in {"max_iterations", "loop_guard", "error_threshold", "llm_error", "empty_response"} and isinstance(resume_messages, list):
                state = self._build_resume_state(
                    status="paused",
                    reason=str(stop_reason),
                    user_goal=effective_content,
                    messages=resume_messages,
                    disabled_tools=disabled_tools,
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    sender_id=msg.sender_id,
                    metadata=msg.metadata if isinstance(msg.metadata, dict) else {},
                )
                self._set_resume_state(session, state)
            elif stop_reason not in {"exec_approval_pending"}:
                self._set_resume_state(session, None)

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info(f"Response to {msg.channel}:{msg.sender_id}: {preview}")

        session.add_message(
            "user",
            msg.content,
            message_id=message_id,
            sender_id=msg.sender_id,
            channel=msg.channel,
            chat_id=msg.chat_id,
        )
        history_messages = loop_meta.get("history_messages") if isinstance(loop_meta, dict) else None
        assistant_log_metadata = None
        if isinstance(loop_meta, dict):
            last_llm_response = loop_meta.get("last_llm_response")
            if isinstance(last_llm_response, dict) and last_llm_response:
                assistant_log_metadata = copy.deepcopy(last_llm_response)
        if stop_reason != "exec_approval_pending":
            if isinstance(history_messages, list) and history_messages:
                self._append_session_history(session, history_messages)
            elif stop_reason not in {"llm_error", "empty_response"}:
                session.add_message(
                    "assistant",
                    final_content,
                    tools_used=tools_used if tools_used else None,
                )
            self.channel_logs.append(
                session,
                LogEntry(
                    role="assistant",
                    content=final_content,
                    timestamp=datetime.now().isoformat(),
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    metadata=assistant_log_metadata,
                ),
            )
        if self.debug and debug_log:
            session.metadata.setdefault("debug_log", []).extend(debug_log)
        self.sessions.save(session)

        if stop_reason == "exec_approval_pending" and suppress_outbound_for_pending:
            return None

        if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
            return None

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=msg.metadata or {},  # Pass through for channel-specific needs
        )

    async def _consolidate_memory(self, session, archive_all: bool = False) -> bool:
        """Consolidate old messages into MEMORY.md + HISTORY.md.

        Args:
            archive_all: If True, clear all messages and reset session (for /new command).
                       If False, only write to files without modifying session.
        """
        memory = MemoryStore(self.workspace)
        debug_log: list[dict[str, Any]] | None = [] if self.debug else None

        if archive_all:
            old_messages = session.messages
            keep_count = 0
            logger.info(f"Memory consolidation (archive_all): {len(session.messages)} total messages archived")
        else:
            keep_count = self.memory_window // 2
            if len(session.messages) <= keep_count:
                logger.debug(f"Session {session.key}: No consolidation needed (messages={len(session.messages)}, keep={keep_count})")
                return True

            messages_to_process = len(session.messages) - session.last_consolidated
            if messages_to_process <= 0:
                logger.debug(f"Session {session.key}: No new messages to consolidate (last_consolidated={session.last_consolidated}, total={len(session.messages)})")
                return True

            old_messages = session.messages[session.last_consolidated:-keep_count]
            if not old_messages:
                return True
            logger.info(f"Memory consolidation started: {len(session.messages)} total, {len(old_messages)} new to consolidate, {keep_count} keep")

        lines = []
        for m in old_messages:
            role = str(m.get("role") or "").lower()
            content = str(m.get("content") or "").strip()

            # Tool observations can be verbose; keep compact signal for consolidation.
            if role == "tool":
                if not content:
                    continue
                tool_name = m.get("name") or "tool"
                compact = self._compact_tool_content(
                    content.replace("\n", " "),
                    max_chars=self.MEMORY_TOOL_RESULT_MAX_CHARS,
                )
                lines.append(f"[{m.get('timestamp', '?')[:16]}] TOOL({tool_name}): {compact}")
                continue

            if role == "assistant" and m.get("tool_calls") and not content:
                call_names = [
                    tc.get("function", {}).get("name", "tool")
                    for tc in m.get("tool_calls", [])
                    if isinstance(tc, dict)
                ]
                content = f"[tool calls] {', '.join(call_names)}" if call_names else "[tool calls]"

            if not content:
                continue
            tools = f" [tools: {', '.join(m['tools_used'])}]" if m.get("tools_used") else ""
            lines.append(f"[{m.get('timestamp', '?')[:16]}] {role.upper()}{tools}: {content}")
        conversation = "\n".join(lines)
        current_memory = memory.read_long_term()

        prompt = f"""Process this conversation and call the save_memory tool with your consolidation.

The save_memory tool requires:
1. history_entry: A paragraph (2-5 sentences) summarizing key events/decisions/topics. Start with [YYYY-MM-DD HH:MM]. Include enough detail to be useful when found by grep search later.
2. memory_update: The full updated long-term memory markdown. Add any new facts (user preferences, project context, technical decisions, tools/services used), or return unchanged if nothing new.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{conversation}
"""

        try:
            consolidation_messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a memory consolidation agent. Call the save_memory tool with your "
                        "consolidation of the conversation."
                    ),
                },
                {"role": "user", "content": prompt},
            ]
            response = await self._chat_with_optional_timeout(
                messages=consolidation_messages,
                tools=_SAVE_MEMORY_TOOL,
                model=self.model,
            )
            self._record_debug_entry(
                debug_log,
                consolidation_messages,
                _SAVE_MEMORY_TOOL,
                response,
                source="consolidation",
            )
            if not response.has_tool_calls:
                logger.warning("Memory consolidation: LLM did not call save_memory, skipping")
                return False

            tool_args = {}
            for tc in response.tool_calls:
                if tc.name == "save_memory":
                    tool_args = tc.arguments or {}
                    break
            if not tool_args and response.tool_calls:
                tool_args = response.tool_calls[0].arguments or {}

            if entry := tool_args.get("history_entry"):
                if not isinstance(entry, str):
                    entry = json.dumps(entry, ensure_ascii=False)
                memory.append_history(entry)
            if update := tool_args.get("memory_update"):
                if not isinstance(update, str):
                    update = json.dumps(update, ensure_ascii=False)
                if update != current_memory:
                    memory.write_long_term(update)

            if archive_all:
                session.last_consolidated = 0
            else:
                session.last_consolidated = len(session.messages) - keep_count
            logger.info(f"Memory consolidation done: {len(session.messages)} messages, last_consolidated={session.last_consolidated}")
            return True
        except Exception as e:
            logger.error(f"Memory consolidation failed: {e}")
            return False
        finally:
            if self.debug and debug_log and not archive_all:
                session.metadata.setdefault("debug_log", []).extend(debug_log)
                self.sessions.save(session)

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """
        Process a message directly (for CLI usage).

        Args:
            content: The message content.
            session_key: Session identifier (overrides channel:chat_id for session lookup).
            channel: Source channel (for tool context routing).
            chat_id: Source chat ID (for tool context routing).

        Returns:
            The agent's response.
        """
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content,
            metadata=dict(metadata or {}),
        )

        response = await self._process_message(msg, session_key=session_key)
        return response.content if response else ""
