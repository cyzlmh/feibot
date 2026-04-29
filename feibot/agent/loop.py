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
from feibot.agent.memory import MemoryStore
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
        "/new — Clear history and start a fresh conversation\n"
        "/go (or: 继续 / continue) — Resume the previous paused task\n"
        "/stop — Cancel the currently running task\n"
        "/help — Show this help message\n"
        "/chatid — Show your user ID and current chat ID\n"
        "/fork [label] — Open a Feishu subtask chat (inherits current context)\n"
        "/spawn [label] — Open a Feishu subtask chat (starts with fresh context)"
    )
    MANAGEMENT_DENY_PATTERNS = [
        r"\blaunchctl\b[^\n]*\bai\.[A-Za-z0-9_.-]+\.gateway\b",
        r"\bmanage\.sh\b[^\n]*\b(?:start|stop|restart|install|uninstall)\b",
        r"\bsystemctl\b[^\n]*\b(?:feibot|gateway|outstreet)\b",
    ]

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 100,
        max_consecutive_tool_errors: int = 10,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        memory_window: int = 50,
        brave_api_key: str | None = None,
        skills_env: dict[str, str] | None = None,
        skills_my_source: str = "",
        exec_config: Any | None = None,
        feishu_config: Any | None = None,
        madame_config: Any | None = None,
        cron_service: Any | None = None,

        writable_dirs: list[str] | None = None,
        allowed_hosts: list[str] | None = None,
        session_manager: SessionManager | None = None,
        debug: bool = False,
        agent_name: str = "feibot",
        disabled_tools: list[str] | None = None,
        disable_all_tools: bool = False,
        include_skills: bool = True,
        include_long_term_memory: bool = True,
        config_path: Path | None = None,
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
        self.skills_my_source = str(skills_my_source or "").strip()
        self.exec_config = exec_config or ExecToolConfig()
        self.feishu_config = feishu_config
        self.madame_config = madame_config
        self._madame_enabled = bool(getattr(self.madame_config, "enabled", False))
        self._feishu_base_url = "https://open.feishu.cn"
        self.cron_service = cron_service
        self.writable_dirs = writable_dirs
        self.allowed_hosts = allowed_hosts
        self.debug = debug
        self.agent_name = agent_name
        self.config_path = config_path
        self.disable_all_tools = bool(disable_all_tools)
        self.include_skills = bool(include_skills)
        self.include_long_term_memory = bool(include_long_term_memory)
        self.default_disabled_tools = {
            str(name).strip()
            for name in (disabled_tools or [])
            if str(name).strip()
        }
        self.llm_timeout = float(llm_timeout) if llm_timeout and llm_timeout > 0 else None
        self.context = ContextBuilder(
            workspace,
            include_skills=self.include_skills,
            include_long_term_memory=self.include_long_term_memory,
            skills_env=self.skills_env,
        )
        self.sessions = session_manager or SessionManager(workspace / "sessions")
        self.channel_logs = ChannelLogStore(workspace / "logs")
        self.tools = ToolRegistry()
        self.madame_controller = None

        self._running = False
        self._active_tasks: dict[str, list[asyncio.Task[None]]] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._consolidating: set[str] = set()
        self._consolidation_tasks: set[asyncio.Task] = set()
        self._consolidation_locks: dict[str, asyncio.Lock] = {}
        self._feishu_default_member_open_id = ""
        if self.feishu_config and getattr(self.feishu_config, "allow_from", None):
            allow_from_ids = extract_allow_from_open_ids(list(getattr(self.feishu_config, "allow_from", []) or []))
            if allow_from_ids:
                self._feishu_default_member_open_id = allow_from_ids[0]
        self._register_default_tools()
        self._init_madame_controller()

    def _commands_help_text(self) -> str:
        text = self.COMMANDS_HELP_TEXT
        if self.madame_controller is not None:
            text += (
                "\n\nAgent management (madame):\n"
                "/agent list — List all agent instances\n"
                "/agent status <id> — Show agent status\n"
                "/agent create --name <id> --mode <agent|chat> — Create an agent\n"
                "/agent start|stop|restart <id> — Control an agent\n"
                "/agent restart all — Restart all agents\n"
                "/agent archive <id> — Archive an agent\n"
                "/agent pool list|add|remove — Manage credential pool\n"
                "/agent cron <list|add|runs|remove|enable|disable|run> — Manage scheduled jobs\n"
                "\nSkills management (madame):\n"
                "/skillhub list|find [query]|install <pkg>|uninstall <name>\n"
                "/skill list|show <id>|add <id> <skills>|remove <id> <skills>|sync <id>|clear <id>"
            )
        return text

    def _init_madame_controller(self) -> None:
        if not self._madame_enabled:
            return

        from feibot.madame.controller import AgentMadameController

        registry_raw = str(getattr(self.madame_config, "registry_path", "") or "").strip()
        if registry_raw:
            registry_path = Path(registry_raw).expanduser()
            if not registry_path.is_absolute():
                registry_path = (self.workspace / registry_path).resolve()
        else:
            registry_path = (self.workspace / "madame" / "agents_registry.json").resolve()

        manage_script_raw = str(getattr(self.madame_config, "manage_script", "") or "").strip()
        manage_script = Path(manage_script_raw).expanduser().resolve() if manage_script_raw else None

        base_dir_template = str(getattr(self.madame_config, "base_dir_template", "") or "").strip()
        backup_dir_raw = str(getattr(self.madame_config, "backup_dir", "") or "").strip()
        backup_dir = Path(backup_dir_raw).expanduser().resolve() if backup_dir_raw else None

        try:
            self.madame_controller = AgentMadameController(
                workspace=self.workspace,
                repo_dir=Path(__file__).resolve().parents[2],
                registry_path=registry_path,
                madame_runtime_id=str(getattr(self.madame_config, "runtime_id", "") or self.agent_name),
                manage_script=manage_script,
                base_dir_template=base_dir_template,
                backup_dir=backup_dir,
                my_skills_source=self.skills_my_source,
            )
        except Exception as e:
            logger.error(f"Failed to initialize madame controller: {e}")
            self.madame_controller = None

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        writable_dirs = list(self.writable_dirs or [str(self.workspace)])
        self.tools.register(ReadFileTool())
        self.tools.register(WriteFileTool(writable_dirs=writable_dirs))
        self.tools.register(EditFileTool(writable_dirs=writable_dirs))
        self.tools.register(ListDirTool())
        self.tools.register(FindFileTool(base_dir=self.workspace))
        self.tools.register(GrepTextTool(base_dir=self.workspace))

        # Shell tool
        exec_tool = ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            writable_dirs=writable_dirs,
            allowed_hosts=self.allowed_hosts,
            path_append=self.exec_config.path_append,
            injected_env=self.skills_env,
        )
        self.tools.register(exec_tool)

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

    async def _open_fork_chat(
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
            return "Error: /fork is only supported in Feishu chats."
        app_id = str(getattr(self.feishu_config, "app_id", "") if self.feishu_config else "").strip()
        app_secret = str(getattr(self.feishu_config, "app_secret", "") if self.feishu_config else "").strip()
        if not app_id or not app_secret:
            return "Error: Feishu credentials not configured for /fork (channels.feishu.app_id/app_secret)."

        user_open_id = self._resolve_fork_user_open_id(sender_id)
        if not user_open_id:
            return (
                "Error: Cannot determine Feishu user open_id for /fork. "
                "Need current sender open_id or channels.feishu.allow_from[0]."
            )

        chat_name = self._build_fork_chat_name(label=label)
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

    async def _open_spawn_chat(
        self,
        *,
        label: str | None,
        origin_chat_id: str,
        sender_id: str,
        channel: str,
    ) -> str:
        """Spawn a new Feishu session without cloning context."""
        if channel != "feishu":
            return "Error: /spawn is only supported in Feishu chats."
        app_id = str(getattr(self.feishu_config, "app_id", "") if self.feishu_config else "").strip()
        app_secret = str(getattr(self.feishu_config, "app_secret", "") if self.feishu_config else "").strip()
        if not app_id or not app_secret:
            return "Error: Feishu credentials not configured for /spawn (channels.feishu.app_id/app_secret)."

        user_open_id = self._resolve_fork_user_open_id(sender_id)
        if not user_open_id:
            return (
                "Error: Cannot determine Feishu user open_id for /spawn. "
                "Need current sender open_id or channels.feishu.allow_from[0]."
            )

        chat_name = self._build_fork_chat_name(label=label)
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

        # Create empty session (no context cloning)
        target_session_key = f"feishu:{chat_id}"
        target = self.sessions.rotate(target_session_key)
        target.updated_at = datetime.now()
        self.sessions.save(target)

        logger.info(
            "Spawned Feishu subtask chat {} (origin={}, user={})",
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

    def _resolve_fork_user_open_id(self, sender_id: str) -> str:
        sender = str(sender_id or "").strip()
        if sender.startswith("ou_"):
            return sender
        fallback = str(self._feishu_default_member_open_id or "").strip()
        if fallback.startswith("ou_"):
            return fallback
        return ""

    def _build_fork_chat_name(self, label: str | None) -> str:
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

            if self.disable_all_tools:
                tool_defs = []
            else:
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
        """Cancel all active tasks for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        wait_tasks: list[asyncio.Task[Any]] = list(dict.fromkeys(tasks))
        cancelled = sum(1 for t in wait_tasks if not t.done() and t.cancel())
        if wait_tasks:
            await asyncio.gather(*wait_tasks, return_exceptions=True)

        lock = self._session_locks.get(msg.session_key)
        if lock is not None and lock.locked():
            while lock.locked():
                await asyncio.sleep(0.01)
        if lock is not None and not lock.locked():
            self._prune_session_lock(msg.session_key, lock)

        total = cancelled
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
        disabled_tools: set[str] | None = (
            set(self.default_disabled_tools) if self.default_disabled_tools else None
        )
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
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="No active task to stop.",
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
                content=self._commands_help_text(),
            )
        if cmd == "/agent":
            if self.madame_controller is None:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="Madame commands are disabled for this agent.",
                )
            if hasattr(self.madame_controller, "bind_runtime"):
                self.madame_controller.bind_runtime(
                    loop=asyncio.get_running_loop(),
                    cron_service=self.cron_service,
                )
            madame_reply = await asyncio.to_thread(self.madame_controller.execute, cmd_args)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=madame_reply,
            )
        if cmd == "/skillhub":
            if self.madame_controller is None:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="Skill hub commands are disabled for this agent.",
                )
            from feibot.madame.controller import AgentMadameController

            tokens = AgentMadameController._split(cmd_args)
            madame_reply = await asyncio.to_thread(
                self.madame_controller._skills_hub_command, tokens
            )
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=madame_reply,
            )
        if cmd == "/skill":
            if self.madame_controller is None:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="Skill commands are disabled for this agent.",
                )
            from feibot.madame.controller import AgentMadameController

            tokens = AgentMadameController._split(cmd_args)
            madame_reply = await asyncio.to_thread(
                self.madame_controller._skills_agent_command, tokens
            )
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=madame_reply,
            )
            from feibot.madame.controller import AgentMadameController
            tokens = AgentMadameController._split(cmd_args)
            if not tokens:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=self.madame_controller._skills_help_text(),
                )
            madame_reply = await asyncio.to_thread(self.madame_controller._skills_command, tokens)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=madame_reply,
            )
        if cmd == "/chatid":
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=f"User ID: `{msg.sender_id}`\nChat ID: `{msg.chat_id}`",
            )
        if cmd == "/fork":
            spawn_result = await self._open_fork_chat(
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
        if cmd == "/spawn":
            spawn_result = await self._open_spawn_chat(
                label=cmd_args or None,
                origin_chat_id=msg.chat_id,
                sender_id=msg.sender_id,
                channel=msg.channel,
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
            "/fork",
            "/agent",
            "/skills",
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
        else:
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
