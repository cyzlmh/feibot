"""Agent loop: the core processing engine."""

import asyncio
import copy
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from feibot.agent.context import ContextBuilder
from feibot.agent.exec_approval import (
    ApprovalDecision,
    ExecApprovalManager,
    ExecApprovalRequest,
    ExecApprovalResolution,
)
from feibot.agent.memory import MemoryStore
from feibot.agent.sim_auth import SimAuthResolver
from feibot.agent.subagent import SubagentManager
from feibot.agent.tools.cron import CronTool
from feibot.agent.tools.feishu import (
    FeishuAppScopesTool,
    FeishuBitableCreateAppTool,
    FeishuBitableCreateFieldTool,
    FeishuBitableCreateRecordTool,
    FeishuBitableGetMetaTool,
    FeishuBitableGetRecordTool,
    FeishuBitableListFieldsTool,
    FeishuBitableListRecordsTool,
    FeishuBitableUpdateRecordTool,
    FeishuDocTool,
    FeishuDriveTool,
    FeishuPermTool,
    FeishuSendFileTool,
    FeishuWikiTool,
)
from feibot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from feibot.agent.tools.message import MessageTool
from feibot.agent.tools.registry import ToolRegistry
from feibot.agent.tools.search import FindFileTool, GrepTextTool
from feibot.agent.tools.shell import ExecTool
from feibot.agent.tools.spawn import SpawnTool
from feibot.agent.tools.web import WebFetchTool, WebSearchTool
from feibot.bus.events import InboundMessage, OutboundMessage
from feibot.bus.queue import MessageBus
from feibot.providers.base import LLMProvider
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


@dataclass
class PendingExecContinuation:
    """In-memory resume context captured when exec approval pauses a loop."""

    approval_id: str
    session_key: str
    channel: str
    chat_id: str
    sender_id: str
    user_goal: str
    initial_messages: list[dict[str, Any]]
    base_history_messages: list[dict[str, Any]]
    disabled_tools: set[str] | None
    metadata: dict[str, Any]


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
    SPAWN_CHILD_SESSION_METADATA_KEY = "spawn_child_session"
    COMMANDS_HELP_TEXT = (
        "🐈 feibot commands:\n"
        "/new — Start a new conversation\n"
        "/stop — Stop the current task\n"
        "/help — Show available commands\n"
        "/sp [label] — Open a Feishu subtask group chat"
    )
    _MISLEADING_SUCCESS_REASONS = {
        "success",
        "ok",
        "passed",
        "approved",
        "成功",
        "处理成功",
        "通过",
    }

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 20,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        memory_window: int = 50,
        brave_api_key: str | None = None,
        exec_config: Any | None = None,
        feishu_config: Any | None = None,
        cron_service: Any | None = None,

        restrict_to_workspace: bool = False,
        allowed_dirs: list[str] | None = None,
        session_manager: SessionManager | None = None,
        debug: bool = False,
    ):
        from feibot.config.schema import ExecToolConfig

        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.memory_window = memory_window
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.feishu_config = feishu_config
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self.allowed_dirs = allowed_dirs
        self.debug = debug
        self.exec_approvals = ExecApprovalManager(
            enabled=bool(getattr(self.exec_config, "approval_enabled", True)),
            timeout_sec=int(getattr(self.exec_config, "approval_timeout_sec", 120) or 120),
            approvers=list(getattr(self.exec_config, "approval_approvers", []) or []),
        )
        self.sim_auth_resolver = SimAuthResolver(
            verify_url=str(getattr(self.exec_config, "approval_sim_auth_url", "") or "").strip(),
            api_key=str(getattr(self.exec_config, "approval_sim_auth_api_key", "") or "").strip(),
            timeout_sec=int(getattr(self.exec_config, "approval_sim_auth_timeout_sec", 90) or 90),
            extra_headers=dict(getattr(self.exec_config, "approval_sim_auth_extra_headers", {}) or {}),
            cmcc_host=str(getattr(self.exec_config, "approval_sim_auth_host", "") or "").strip(),
            cmcc_send_auth_path=str(
                getattr(self.exec_config, "approval_sim_auth_send_auth_path", "") or ""
            ).strip(),
            cmcc_get_result_path=str(
                getattr(self.exec_config, "approval_sim_auth_get_result_path", "") or ""
            ).strip(),
            cmcc_ap_id=str(getattr(self.exec_config, "approval_sim_auth_ap_id", "") or "").strip(),
            cmcc_app_id=str(getattr(self.exec_config, "approval_sim_auth_app_id", "") or "").strip(),
            cmcc_private_key=str(
                getattr(self.exec_config, "approval_sim_auth_private_key", "") or ""
            ).strip(),
            cmcc_msisdn=str(getattr(self.exec_config, "approval_sim_auth_msisdn", "") or "").strip(),
            cmcc_template_id=str(
                getattr(self.exec_config, "approval_sim_auth_template_id", "") or ""
            ).strip(),
            cmcc_callback_url=str(
                getattr(self.exec_config, "approval_sim_auth_callback_url", "") or ""
            ).strip(),
            cmcc_callback_timeout_sec=int(
                getattr(self.exec_config, "approval_sim_auth_callback_timeout_sec", 65) or 65
            ),
            cmcc_poll_interval_sec=float(
                getattr(self.exec_config, "approval_sim_auth_poll_interval_sec", 2.0) or 2.0
            ),
            cmcc_poll_timeout_sec=int(
                getattr(self.exec_config, "approval_sim_auth_poll_timeout_sec", 65) or 65
            ),
            cmcc_callback_listen_host=str(
                getattr(self.exec_config, "approval_sim_auth_callback_listen_host", "") or ""
            ).strip(),
            cmcc_callback_listen_port=int(
                getattr(self.exec_config, "approval_sim_auth_callback_listen_port", 0) or 0
            ),
            cmcc_callback_path=str(
                getattr(self.exec_config, "approval_sim_auth_callback_path", "/callback") or "/callback"
            ).strip(),
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
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )

        self._running = False
        self._active_tasks: dict[str, list[asyncio.Task[None]]] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._consolidating: set[str] = set()
        self._consolidation_tasks: set[asyncio.Task] = set()
        self._consolidation_locks: dict[str, asyncio.Lock] = {}
        self._approval_execution_tasks: set[asyncio.Task[None]] = set()
        self._approval_continuations: dict[str, PendingExecContinuation] = {}
        self._sim_auth_tasks: set[asyncio.Task[None]] = set()
        self._sim_auth_pending_ids: set[str] = set()
        self._sim_auth_warned_missing_config = False
        self._register_default_tools()

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
            approval_manager=self.exec_approvals,
        ))

        # Web tools
        self.tools.register(WebSearchTool(api_key=self.brave_api_key))
        self.tools.register(WebFetchTool())
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

        fs_cfg = self.feishu_config
        default_receive_id = ""
        if fs_cfg and getattr(fs_cfg, "allow_from", None):
            default_receive_id = fs_cfg.allow_from[0]

        # Message tool
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(
            SpawnTool(
                manager=self.subagents,
                feishu_app_id=getattr(fs_cfg, "app_id", "") if fs_cfg else "",
                feishu_app_secret=getattr(fs_cfg, "app_secret", "") if fs_cfg else "",
                feishu_default_member_open_id=default_receive_id,
            )
        )

        # Feishu file tool
        self.tools.register(
            FeishuSendFileTool(
                app_id=getattr(fs_cfg, "app_id", "") if fs_cfg else "",
                app_secret=getattr(fs_cfg, "app_secret", "") if fs_cfg else "",
                default_receive_id=default_receive_id,
                default_receive_id_type="open_id",
                allowed_dir=allowed_dir,
            )
        )
        self.tools.register(
            FeishuDocTool(
                app_id=getattr(fs_cfg, "app_id", "") if fs_cfg else "",
                app_secret=getattr(fs_cfg, "app_secret", "") if fs_cfg else "",
                owner_open_id=default_receive_id,
                wiki_space_id=getattr(fs_cfg, "wiki_space_id", "") if fs_cfg else "",
                wiki_parent_node_token=getattr(fs_cfg, "wiki_parent_node_token", "") if fs_cfg else "",
                auto_chunk_threshold_chars=(
                    int(getattr(fs_cfg, "doc_write_auto_chunk_threshold_chars", 6000) or 0)
                    if fs_cfg
                    else 6000
                ),
            )
        )
        self.tools.register(
            FeishuWikiTool(
                app_id=getattr(fs_cfg, "app_id", "") if fs_cfg else "",
                app_secret=getattr(fs_cfg, "app_secret", "") if fs_cfg else "",
            )
        )
        self.tools.register(
            FeishuDriveTool(
                app_id=getattr(fs_cfg, "app_id", "") if fs_cfg else "",
                app_secret=getattr(fs_cfg, "app_secret", "") if fs_cfg else "",
            )
        )
        self.tools.register(
            FeishuAppScopesTool(
                app_id=getattr(fs_cfg, "app_id", "") if fs_cfg else "",
                app_secret=getattr(fs_cfg, "app_secret", "") if fs_cfg else "",
            )
        )
        self.tools.register(
            FeishuPermTool(
                app_id=getattr(fs_cfg, "app_id", "") if fs_cfg else "",
                app_secret=getattr(fs_cfg, "app_secret", "") if fs_cfg else "",
            )
        )
        self.tools.register(
            FeishuBitableGetMetaTool(
                app_id=getattr(fs_cfg, "app_id", "") if fs_cfg else "",
                app_secret=getattr(fs_cfg, "app_secret", "") if fs_cfg else "",
            )
        )
        self.tools.register(
            FeishuBitableListFieldsTool(
                app_id=getattr(fs_cfg, "app_id", "") if fs_cfg else "",
                app_secret=getattr(fs_cfg, "app_secret", "") if fs_cfg else "",
            )
        )
        self.tools.register(
            FeishuBitableListRecordsTool(
                app_id=getattr(fs_cfg, "app_id", "") if fs_cfg else "",
                app_secret=getattr(fs_cfg, "app_secret", "") if fs_cfg else "",
            )
        )
        self.tools.register(
            FeishuBitableGetRecordTool(
                app_id=getattr(fs_cfg, "app_id", "") if fs_cfg else "",
                app_secret=getattr(fs_cfg, "app_secret", "") if fs_cfg else "",
            )
        )
        self.tools.register(
            FeishuBitableCreateRecordTool(
                app_id=getattr(fs_cfg, "app_id", "") if fs_cfg else "",
                app_secret=getattr(fs_cfg, "app_secret", "") if fs_cfg else "",
            )
        )
        self.tools.register(
            FeishuBitableUpdateRecordTool(
                app_id=getattr(fs_cfg, "app_id", "") if fs_cfg else "",
                app_secret=getattr(fs_cfg, "app_secret", "") if fs_cfg else "",
            )
        )
        self.tools.register(
            FeishuBitableCreateAppTool(
                app_id=getattr(fs_cfg, "app_id", "") if fs_cfg else "",
                app_secret=getattr(fs_cfg, "app_secret", "") if fs_cfg else "",
            )
        )
        self.tools.register(
            FeishuBitableCreateFieldTool(
                app_id=getattr(fs_cfg, "app_id", "") if fs_cfg else "",
                app_secret=getattr(fs_cfg, "app_secret", "") if fs_cfg else "",
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
        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(
                channel,
                chat_id,
                sender_id,
                session_key=session_key or f"{channel}:{chat_id}",
            )
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

    def _approval_mode(self, channel: str, *, risk_level: str = "confirm") -> str:
        if risk_level == "hard-danger":
            hard_mode_raw = str(
                getattr(self.exec_config, "approval_hard_danger_mode", "") or ""
            ).strip().lower()
            mode = hard_mode_raw if hard_mode_raw in {"text", "feishu_card", "sim_auth"} else ""
            if not mode:
                mode = str(getattr(self.exec_config, "approval_mode", "text") or "text").strip().lower()
        else:
            mode = str(getattr(self.exec_config, "approval_mode", "text") or "text").strip().lower()
        if mode not in {"text", "feishu_card", "sim_auth"}:
            mode = "text"
        if mode == "feishu_card" and channel != "feishu":
            return "text"
        if mode == "sim_auth" and not self.sim_auth_resolver.enabled:
            if not self._sim_auth_warned_missing_config:
                logger.warning(
                    "Exec approval mode is sim_auth but approval_sim_auth_url is empty; falling back to text approval."
                )
                self._sim_auth_warned_missing_config = True
            return "text"
        return mode

    def _build_exec_approval_card(self, request: ExecApprovalRequest) -> dict[str, Any]:
        command_block = self._format_command_as_markdown(request.command)
        command_preview = self._build_command_preview(request.command)
        risk_label = "hard-danger" if str(request.risk_level).strip().lower() == "hard-danger" else "confirm"
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
        card_enabled = self._is_feishu_card_approval_enabled(request)
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

    def _is_feishu_card_approval_enabled(self, request: ExecApprovalRequest) -> bool:
        return self._approval_mode(
            request.channel,
            risk_level=str(request.risk_level or "confirm"),
        ) == "feishu_card"

    def _is_sim_auth_approval_enabled(self, request: ExecApprovalRequest) -> bool:
        return self._approval_mode(
            request.channel,
            risk_level=str(request.risk_level or "confirm"),
        ) == "sim_auth"

    def _capture_exec_approval_continuation(
        self,
        *,
        approval_id: str,
        msg: InboundMessage,
        user_goal: str,
        loop_meta: dict[str, Any],
        disabled_tools: set[str] | None,
    ) -> None:
        """Store resumable context so an approved exec can continue the blocked loop."""
        resume_messages = loop_meta.get("resume_messages")
        base_history_messages = loop_meta.get("resume_base_history_messages")
        if not isinstance(resume_messages, list) or not isinstance(base_history_messages, list):
            return

        metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
        self._approval_continuations[approval_id] = PendingExecContinuation(
            approval_id=approval_id,
            session_key=msg.session_key,
            channel=msg.channel,
            chat_id=msg.chat_id,
            sender_id=msg.sender_id,
            user_goal=user_goal,
            initial_messages=copy.deepcopy(resume_messages),
            base_history_messages=copy.deepcopy(base_history_messages),
            disabled_tools=set(disabled_tools) if disabled_tools else None,
            metadata=dict(metadata),
        )

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
        self._approval_execution_tasks.add(task)

        def _cleanup(t: asyncio.Task[None]) -> None:
            self._approval_execution_tasks.discard(t)

        task.add_done_callback(_cleanup)

    async def _request_sim_auth_decision(
        self,
        request: ExecApprovalRequest,
    ) -> tuple[ApprovalDecision, str]:
        """Request decision from SIM-auth resolver."""
        verdict = await self.sim_auth_resolver.verify(request)
        return verdict.decision, self._normalize_sim_auth_reason(verdict.decision, verdict.reason)

    @classmethod
    def _normalize_sim_auth_reason(cls, decision: ApprovalDecision, reason: str) -> str:
        text = str(reason or "").strip()
        if decision != "deny":
            return text
        normalized = text.lower().strip(" .!?,;:，。！？；：")
        if normalized and normalized not in cls._MISLEADING_SUCCESS_REASONS:
            return text
        return "SIM auth rejected."

    async def _record_exec_deny_history(
        self,
        resolution: ExecApprovalResolution,
        *,
        denial_content: str,
    ) -> None:
        request = resolution.request
        lock = self._get_session_lock(request.session_key)
        try:
            async with lock:
                continuation = self._approval_continuations.pop(request.id, None)
                session_key = request.session_key
                session = self.sessions.get_or_create(session_key)
                if continuation is not None:
                    denied_result = f"Error: exec approval denied (ID: {request.id})."
                    self._replace_exec_approval_pending_result(
                        continuation.initial_messages, request.id, denied_result
                    )
                    self._replace_exec_approval_pending_result(
                        continuation.base_history_messages, request.id, denied_result
                    )
                    session = self.sessions.get_or_create(continuation.session_key)
                    session_key = continuation.session_key
                    self._append_session_history(session, continuation.base_history_messages)
                    # Set pending_task so user can continue after denial
                    session.metadata["pending_task"] = continuation.user_goal

                session.add_message("assistant", denial_content, tools_used=["exec"])
                self.channel_logs.append(
                    session_key,
                    LogEntry(
                        role="assistant",
                        content=denial_content,
                        timestamp=datetime.now().isoformat(),
                        channel=request.channel,
                        chat_id=request.chat_id,
                    ),
                )
                self.sessions.save(session)
        finally:
            self._prune_session_lock(request.session_key, lock)

    def _schedule_sim_auth_after_pending(self, request: ExecApprovalRequest) -> None:
        if not self._is_sim_auth_approval_enabled(request):
            return
        if request.id in self._sim_auth_pending_ids:
            return
        self._sim_auth_pending_ids.add(request.id)
        task = asyncio.create_task(self._run_sim_auth_after_pending(request))
        self._sim_auth_tasks.add(task)

        def _cleanup(t: asyncio.Task[None]) -> None:
            self._sim_auth_tasks.discard(t)
            self._sim_auth_pending_ids.discard(request.id)

        task.add_done_callback(_cleanup)

    async def _run_sim_auth_after_pending(self, request: ExecApprovalRequest) -> None:
        decision, reason = await self._request_sim_auth_decision(request)
        resolution, error = self.exec_approvals.resolve(
            approval_id=request.id,
            decision=decision,
            resolved_by=request.requester_id,
        )
        if resolution is None:
            err_text = str(error or "").strip()
            if "not found" in err_text.lower():
                logger.debug(
                    "SimAuth ignored stale approval {}: {}",
                    request.id,
                    err_text or "request missing",
                )
                return
            logger.warning(
                "SimAuth resolution failed for approval {}: {}",
                request.id,
                err_text or "unknown error",
            )
            if reason:
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=request.channel,
                        chat_id=request.chat_id,
                        content=f"❌ SimAuth verification failed (ID: {request.id}): {reason}",
                        metadata={"_suppress_progress": True, "_exec_approval_id": request.id},
                    )
                )
            return

        if resolution.decision == "deny":
            content = f"❌ SimAuth denied exec approval (ID: {resolution.request.id})."
            if reason:
                content = f"{content}\nReason: {reason}"
            await self._record_exec_deny_history(
                resolution,
                denial_content=content,
            )
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=request.channel,
                    chat_id=request.chat_id,
                    content=content,
                    metadata={"_suppress_progress": True, "_exec_approval_id": request.id},
                )
            )
            return

        self._schedule_exec_after_approval(resolution)

    async def _run_approved_exec(self, resolution: ExecApprovalResolution) -> None:
        request = resolution.request
        lock = self._get_session_lock(request.session_key)
        try:
            async with lock:
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

                continuation = self._approval_continuations.pop(request.id, None)
                if continuation is None:
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

                self._replace_exec_approval_pending_result(
                    continuation.initial_messages, request.id, result_text
                )
                self._replace_exec_approval_pending_result(
                    continuation.base_history_messages, request.id, result_text
                )

                session = self.sessions.get_or_create(continuation.session_key)
                self._append_session_history(session, continuation.base_history_messages)

                message_tool = self.tools.get("message")
                if isinstance(message_tool, MessageTool):
                    message_tool.start_turn()

                async def _resume_progress(content: str, *, tool_hint: bool = False) -> None:
                    if bool(continuation.metadata.get("_suppress_progress")):
                        return
                    meta = dict(continuation.metadata)
                    meta["_progress"] = True
                    meta["_tool_hint"] = tool_hint
                    meta["_exec_approval_id"] = request.id
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=continuation.channel,
                        chat_id=continuation.chat_id,
                        content=content,
                        metadata=meta,
                    ))

                final_content, tools_used, loop_meta = await self._run_agent_loop(
                    continuation.initial_messages,
                    user_goal=continuation.user_goal,
                    on_progress=_resume_progress,
                    disabled_tools=continuation.disabled_tools,
                )

                stop_reason = str(loop_meta.get("stopped_reason") or "")
                if stop_reason == "exec_approval_pending":
                    next_pending_id = str(loop_meta.get("pending_approval_id") or "").strip()
                    if next_pending_id:
                        followup_request = self.exec_approvals.get_request(next_pending_id)
                        resume_messages = loop_meta.get("resume_messages")
                        resume_base_history = loop_meta.get("resume_base_history_messages")
                        if (
                            followup_request
                            and isinstance(resume_messages, list)
                            and isinstance(resume_base_history, list)
                        ):
                            self._approval_continuations[next_pending_id] = PendingExecContinuation(
                                approval_id=next_pending_id,
                                session_key=continuation.session_key,
                                channel=continuation.channel,
                                chat_id=continuation.chat_id,
                                sender_id=continuation.sender_id,
                                user_goal=continuation.user_goal,
                                initial_messages=copy.deepcopy(resume_messages),
                                base_history_messages=copy.deepcopy(resume_base_history),
                                disabled_tools=set(continuation.disabled_tools) if continuation.disabled_tools else None,
                                metadata=dict(continuation.metadata),
                            )
                            await self._publish_exec_approval_prompt(followup_request)
                            self._schedule_sim_auth_after_pending(followup_request)
                    self.sessions.save(session)
                    return

                if stop_reason in {"max_iterations", "loop_guard", "error_threshold"}:
                    session.metadata["pending_task"] = loop_meta.get("pending_task", continuation.user_goal)
                else:
                    session.metadata.pop("pending_task", None)

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
                    continuation.session_key,
                    LogEntry(
                        role="assistant",
                        content=final_content,
                        timestamp=datetime.now().isoformat(),
                        channel=continuation.channel,
                        chat_id=continuation.chat_id,
                    ),
                )
                self.sessions.save(session)

                if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
                    return
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=continuation.channel,
                        chat_id=continuation.chat_id,
                        content=final_content,
                        metadata=continuation.metadata,
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
            "spawn": ["label", "task"],
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
            "feishu_doc": ["action", "doc_token", "url"],
            "feishu_wiki": ["action", "space_id", "token", "url", "node_token"],
            "feishu_drive": ["action", "folder_token", "file_token", "name", "type"],
            "feishu_app_scopes": [],
            "feishu_perm": ["action", "type", "token", "member_type", "member_id"],
            "feishu_bitable_get_meta": ["url"],
            "feishu_bitable_list_fields": ["app_token", "table_id"],
            "feishu_bitable_list_records": ["app_token", "table_id"],
            "feishu_bitable_get_record": ["record_id", "app_token", "table_id"],
            "feishu_bitable_create_record": ["app_token", "table_id"],
            "feishu_bitable_update_record": ["record_id", "app_token", "table_id"],
            "feishu_bitable_create_app": ["name", "folder_token"],
            "feishu_bitable_create_field": ["field_name", "field_type", "app_token", "table_id"],
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
            preview_key = ""
            preview_val: str | None = None
            if isinstance(args, dict) and args:
                for key in preferred_keys_by_tool.get(tc.name, default_preferred_keys):
                    val = args.get(key)
                    if isinstance(val, str) and val.strip():
                        preview_key = key
                        preview_val = val
                        break
                if preview_val is None:
                    for key, val in args.items():
                        if isinstance(val, str) and val.strip():
                            preview_key = str(key)
                            preview_val = val
                            break

            if isinstance(preview_val, str):
                compact = re.sub(r"\s+", " ", preview_val).strip()
                preview = compact[:60] + "…" if len(compact) > 60 else compact
                preview = preview.replace('"', "'")
                if preview_key:
                    hints.append(f'{tc.name}({preview_key}="{preview}")')
                else:
                    hints.append(f'{tc.name}("{preview}")')
            else:
                hints.append(tc.name)
        return ", ".join(hints)

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
            "如果你希望继续，请回复“继续”。我会基于当前进展继续推进。"
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

        debug_log.append({
            "call_id": f"{source}:{iteration}:{len(debug_log) + 1}",
            "timestamp": datetime.now().isoformat(),
            "source": source,
            "iteration": iteration,
            "request": {
                "model": self.model,
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
            },
        })

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        user_goal: str,
        debug_log: list[dict[str, Any]] | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        disabled_tools: set[str] | None = None,
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

        while iteration < self.max_iterations:
            iteration += 1

            tool_defs = self._filter_tool_definitions(
                self.tools.get_definitions(),
                disabled_tools=disabled_tools,
            )
            response = await self.provider.chat(
                messages=messages,
                tools=tool_defs,
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )

            self._record_debug_entry(debug_log, messages, tool_defs, response, iteration=iteration, source="main_loop")

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
                history_messages.append(assistant_turn)

                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                )

                for tool_call in response.tool_calls:
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
                        loop_meta = {
                            "stopped_reason": "loop_guard",
                            "pending_task": user_goal,
                            "history_messages": history_messages,
                        }
                        return final_content, tools_used, loop_meta

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
                        card_only_mode = bool(
                            request and self._is_feishu_card_approval_enabled(request)
                        )
                        sim_auth_mode = bool(
                            request and self._is_sim_auth_approval_enabled(request)
                        )
                        final_content = (
                            f"Exec approval required (ID: {request.id})."
                            if card_only_mode and request
                            else f"Exec approval pending SimAuth verification (ID: {request.id})."
                            if sim_auth_mode and request
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
                        loop_meta = {
                            "stopped_reason": "exec_approval_pending",
                            "pending_approval_id": pending_approval_id,
                            "history_messages": history_messages,
                            "resume_messages": copy.deepcopy(messages),
                            "resume_base_history_messages": base_history_messages,
                        }
                        return final_content, tools_used, loop_meta

                    if tool_call.name == "spawn" and not self._is_tool_error_result(result):
                        final_content = (result or "").strip() or "Subtask started."
                        history_messages.append({
                            "role": "assistant",
                            "content": final_content,
                            "tools_used": tools_used if tools_used else None,
                        })
                        loop_meta = {
                            "stopped_reason": "spawn_finish",
                            "history_messages": history_messages,
                        }
                        return final_content, tools_used, loop_meta

                    if consecutive_tool_errors >= 3:
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
                        loop_meta = {
                            "stopped_reason": "error_threshold",
                            "pending_task": user_goal,
                            "history_messages": history_messages,
                        }
                        return final_content, tools_used, loop_meta

                    if tool_call.name == "message":
                        message_tool = self.tools.get("message")
                        if isinstance(message_tool, MessageTool) and message_tool.finish_requested:
                            content_arg = tool_call.arguments.get("content")
                            final_content = str(content_arg).strip() if content_arg is not None else ""
                            loop_meta = {
                                "stopped_reason": "message_finish",
                                "history_messages": history_messages,
                            }
                            return final_content or "Message sent.", tools_used, loop_meta
                messages.append({"role": "user", "content": "Reflect on the results and decide next steps."})
            else:
                clean = self._strip_think(response.content)
                if response.finish_reason == "error":
                    logger.error("LLM returned error: {}", (clean or response.content or "")[:200])
                    final_content = clean or response.content or "Sorry, I encountered an error calling the AI model."
                    loop_meta = {
                        "stopped_reason": "llm_error",
                        "history_messages": history_messages,
                    }
                    break

                final_candidate = clean or response.content
                if not final_candidate:
                    final_content = "I've completed processing but have no response to give."
                    loop_meta = {
                        "stopped_reason": "empty_response",
                        "history_messages": history_messages,
                    }
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
                history_messages.append(assistant_final)
                loop_meta = {
                    "stopped_reason": "completed",
                    "history_messages": history_messages,
                }
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
            loop_meta = {
                "stopped_reason": "max_iterations",
                "pending_task": user_goal,
                "history_messages": history_messages,
            }

        if "history_messages" not in loop_meta:
            loop_meta["history_messages"] = history_messages
        return final_content, tools_used, loop_meta

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        logger.info("Agent loop started")

        # Start periodic cleanup of expired approvals
        cleanup_task = asyncio.create_task(self._periodic_expired_approval_cleanup())

        try:
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
        finally:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass

    async def _periodic_expired_approval_cleanup(self) -> None:
        """Periodically clean up expired approvals and set pending_task for continuation."""
        while self._running:
            try:
                await asyncio.sleep(30)  # Check every 30 seconds
                if not self._running:
                    break

                expired = self.exec_approvals._prune_expired()
                for request in expired:
                    logger.info("Approval {} expired, cleaning up", request.id)
                    await self._handle_expired_approval(request)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in expired approval cleanup: {}", e)

    async def _handle_expired_approval(self, request: ExecApprovalRequest) -> None:
        """Handle an expired approval: set pending_task for continuation."""
        lock = self._get_session_lock(request.session_key)
        try:
            async with lock:
                continuation = self._approval_continuations.pop(request.id, None)
                if continuation is not None:
                    session = self.sessions.get_or_create(continuation.session_key)
                    # Mark the exec as failed in history
                    denied_result = f"Error: exec approval expired (ID: {request.id})."
                    self._replace_exec_approval_pending_result(
                        continuation.initial_messages, request.id, denied_result
                    )
                    self._replace_exec_approval_pending_result(
                        continuation.base_history_messages, request.id, denied_result
                    )
                    self._append_session_history(session, continuation.base_history_messages)
                    # Set pending_task so user can continue
                    session.metadata["pending_task"] = continuation.user_goal
                    # Add denial message to session
                    denial_content = f"⏱ Exec approval expired (ID: {request.id})."
                    session.add_message("assistant", denial_content, tools_used=["exec"])
                    self.channel_logs.append(
                        continuation.session_key,
                        LogEntry(
                            role="assistant",
                            content=denial_content,
                            timestamp=datetime.now().isoformat(),
                            channel=request.channel,
                            chat_id=request.chat_id,
                        ),
                    )
                    self.sessions.save(session)
                    # Notify user
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=request.channel,
                            chat_id=request.chat_id,
                            content=denial_content,
                            metadata={"_suppress_progress": True, "_exec_approval_id": request.id},
                        )
                    )
        finally:
            self._prune_session_lock(request.session_key, lock)

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
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
        for task in list(self._sim_auth_tasks):
            if not task.done():
                task.cancel()
        self._sim_auth_tasks.clear()
        self._sim_auth_pending_ids.clear()
        try:
            self.sim_auth_resolver.close()
        except Exception:
            logger.exception("Failed to close SimAuth resolver.")
        self._approval_continuations.clear()
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
        metadata = msg.metadata or {}
        session_meta_changed = False
        if bool(metadata.get("_spawn_bootstrap")):
            if not bool(session.metadata.get(self.SPAWN_CHILD_SESSION_METADATA_KEY)):
                session.metadata[self.SPAWN_CHILD_SESSION_METADATA_KEY] = True
                session_meta_changed = True
        if session_meta_changed:
            self.sessions.save(session)

        is_subagent_session = bool(session.metadata.get(self.SPAWN_CHILD_SESSION_METADATA_KEY))
        self._set_tool_context(msg.channel, msg.chat_id, msg.sender_id, session_key=key)
        disabled_tools = {"spawn"} if is_subagent_session else None
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
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"❌ {error}",
                )

            if resolution.decision == "deny":
                deny_content = f"✅ Exec approval denied (ID: {resolution.request.id})."
                await self._record_exec_deny_history(
                    resolution,
                    denial_content=deny_content,
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
            snapshot = list(session.messages[session.last_consolidated:])

            # Prevent raw channel-log backfill from resurrecting pre-/new messages.
            session.metadata["raw_log_sync_after_ts"] = msg.timestamp.isoformat()
            session.clear()
            session.metadata.pop(self.PENDING_FILES_METADATA_KEY, None)
            self.sessions.save(session)
            self.sessions.invalidate(session.key)
            self._schedule_archive_all_consolidation(session.key, snapshot)
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
        if cmd == "/sp":
            spawn_tool = self.tools.get("spawn")
            if not isinstance(spawn_tool, SpawnTool):
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="Error: spawn tool is not available.",
                )
            spawn_result = await spawn_tool.open_session(label=cmd_args or None)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=spawn_result,
            )

        effective_content = msg.content
        if cmd in {"继续", "continue"}:
            pending_task = session.metadata.get("pending_task")
            if isinstance(pending_task, str) and pending_task.strip():
                effective_content = f"Continue unfinished task with current context: {pending_task}"
            else:
                effective_content = "Continue the previous unfinished task with current context."

        unconsolidated = len(session.messages) - session.last_consolidated
        if unconsolidated >= self.memory_window and session.key not in self._consolidating:
            self._consolidating.add(session.key)
            lock = self._get_consolidation_lock(session.key)

            async def _consolidate_and_unlock() -> None:
                try:
                    async with lock:
                        await self._consolidate_memory(session)
                finally:
                    self._consolidating.discard(session.key)
                    self._prune_consolidation_lock(session.key, lock)
                    task = asyncio.current_task()
                    if task is not None:
                        self._consolidation_tasks.discard(task)

            task = asyncio.create_task(_consolidate_and_unlock())
            self._consolidation_tasks.add(task)

        # Record raw inbound message for audit/replay and backfill older unsynced messages.
        self.channel_logs.append(
            key,
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
            after_timestamp=(
                str(session.metadata.get("raw_log_sync_after_ts"))
                if session.metadata.get("raw_log_sync_after_ts")
                else None
            ),
        )
        if synced_count > 0:
            logger.info(f"Backfilled {synced_count} user messages from raw log for session {key}")
            self.sessions.save(session)

        if msg_type == "file":
            file_refs = self._collect_inbound_file_refs(msg)
            cached_files = self._cache_pending_files(session, file_refs)
            final_content = (
                "已收到文件并缓存。\n"
                "请再发一条文字说明你的意图（例如：总结、提取要点、翻译、生成笔记）。"
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
                key,
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
        if msg_type in {"text", "post"} and cmd not in {"/new", "/stop", "/help", "/sp", "/approve"}:
            pending_files = self._pop_pending_files(session)
            effective_content = self._append_file_refs_to_goal(effective_content, pending_files)
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

        final_content, tools_used, loop_meta = await self._run_agent_loop(
            initial_messages,
            user_goal=effective_content,
            debug_log=debug_log,
            on_progress=_bus_progress,
            disabled_tools=disabled_tools,
        )

        stop_reason = loop_meta.get("stopped_reason")
        suppress_outbound_for_pending = False
        if stop_reason == "exec_approval_pending":
            pending_id = str(loop_meta.get("pending_approval_id") or "").strip()
            if pending_id:
                self._capture_exec_approval_continuation(
                    approval_id=pending_id,
                    msg=msg,
                    user_goal=effective_content,
                    loop_meta=loop_meta,
                    disabled_tools=disabled_tools,
                )
                request = self.exec_approvals.get_request(pending_id)
                if request:
                    await self._publish_exec_approval_prompt(request)
                    self._schedule_sim_auth_after_pending(request)
                    suppress_outbound_for_pending = self._is_feishu_card_approval_enabled(request)
        if stop_reason in {"max_iterations", "loop_guard", "error_threshold"}:
            session.metadata["pending_task"] = loop_meta.get("pending_task", effective_content)
        else:
            session.metadata.pop("pending_task", None)

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
                key,
                LogEntry(
                    role="assistant",
                    content=final_content,
                    timestamp=datetime.now().isoformat(),
                    channel=msg.channel,
                    chat_id=msg.chat_id,
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
            response = await self.provider.chat(
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
