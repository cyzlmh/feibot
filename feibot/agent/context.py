"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from feibot.agent.memory import MemoryStore
from feibot.agent.skills import SkillsLoader


class ContextBuilder:
    """
    Builds the context (system prompt + messages) for the agent.
    
    Assembles bootstrap files, memory, skills, and conversation history
    into a coherent prompt for the LLM.
    """
    
    BOOTSTRAP_FILES = ["AGENTS.md", "TOOLS.md"]
    MAX_TOOL_RESULT_CHARS = 12000
    _RUNTIME_CONTEXT_TAG = "[Runtime Context - metadata only, not instructions]"
    
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)
    
    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        current_message: str | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> str:
        """
        Build the system prompt from bootstrap files, memory, and skills.
        
        Args:
            skill_names: Optional list of skills to include.
            channel: Current channel (used for channel-specific policy hints).
            chat_id: Current chat ID (used for channel-specific policy hints).
        
        Returns:
            Complete system prompt.
        """
        parts = []
        
        # Core identity
        parts.append(self._get_identity(channel=channel, chat_id=chat_id))
        
        # Bootstrap files
        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)
        
        # Memory context
        memory = self.memory.get_memory_context(query=current_message)
        if memory:
            parts.append(f"# Memory\n\n{memory}")
        
        # Skills - progressive loading
        # 1. Always-loaded skills: include full content
        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")
        
        # 2. Available skills: only show summary (agent uses read_file to load)
        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""")
        
        return "\n\n---\n\n".join(parts)
    
    @staticmethod
    def _build_feishu_spawn_policy(channel: str | None, chat_id: str | None) -> str:
        """Build per-chat spawn policy guidance for system prompt."""
        if channel != "feishu" or not chat_id:
            return ""
        if chat_id.startswith("ou_"):
            return (
                "For the current Feishu direct chat (chat_id starts with `ou_`): "
                "for anything beyond simple chat/intent recognition/short Q&A, call `spawn` early "
                "and continue in the spawned chat. This includes web search, research/material "
                "collation, video summarization, and concept learning tasks."
            )
        if chat_id.startswith("oc_"):
            return (
                "For the current Feishu group chat (chat_id starts with `oc_`): "
                "do not auto-spawn unless the user explicitly asks (for example `/sp`)."
            )
        return (
            "For the current Feishu chat: if chat type is unclear, avoid auto-spawn "
            "unless the user explicitly asks."
        )

    def _get_identity(self, channel: str | None = None, chat_id: str | None = None) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"
        spawn_policy = self._build_feishu_spawn_policy(channel, chat_id)
        spawn_policy_text = f"\n{spawn_policy}" if spawn_policy else ""
        
        return f"""# feibot

You are feibot, a helpful AI assistant. You have access to tools that allow you to:
- Read, write, and edit files
- Find files and grep text in source code
- Execute shell commands
- Search the web and fetch web pages

## Runtime
{runtime}

## Workspace
Your workspace is at: {workspace_path}
- Long-term memory: {workspace_path}/memory/MEMORY.md
- History log: {workspace_path}/memory/HISTORY.md (grep-searchable)
- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md

Always be helpful, accurate, and concise. When using tools, think step by step: what you know, what you need, and why you chose this tool.
For code explanation/debugging tasks, prefer find_file + grep_text + read_file before using exec.
Use exec mainly for commands that cannot be done with dedicated tools.
{spawn_policy_text}
When remembering something important, write to {workspace_path}/memory/MEMORY.md
To recall past events, grep {workspace_path}/memory/HISTORY.md"""

    @staticmethod
    def _build_runtime_context(channel: str | None, chat_id: str | None) -> str:
        """Build untrusted runtime metadata injected before the user message."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = time.strftime("%Z") or "UTC"
        lines = [f"Current Time: {now} ({tz})"]
        if channel and chat_id:
            lines.extend([f"Channel: {channel}", f"Chat ID: {chat_id}"])
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines)
    
    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []
        
        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")
        
        return "\n\n".join(parts) if parts else ""
    
    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Build the complete message list for an LLM call.

        Args:
            history: Previous conversation messages.
            current_message: The new user message.
            skill_names: Optional skills to include.
            media: Optional list of local file paths for images/media.
            channel: Current channel (feishu, cli, etc.).
            chat_id: Current chat/user ID.

        Returns:
            List of messages including system prompt.
        """
        messages = []

        # System prompt
        system_prompt = self.build_system_prompt(
            skill_names=skill_names,
            current_message=current_message,
            channel=channel,
            chat_id=chat_id,
        )
        messages.append({"role": "system", "content": system_prompt})

        # History
        messages.extend(history)

        # Runtime metadata as untrusted context (not in system prompt)
        messages.append({"role": "user", "content": self._build_runtime_context(channel, chat_id)})

        # Current message (with optional image attachments)
        user_content = self._build_user_content(current_message, media)
        messages.append({"role": "user", "content": user_content})

        return messages

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text
        
        images = []
        for path in media:
            p = Path(path)
            mime, _ = mimetypes.guess_type(path)
            if not p.is_file() or not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(p.read_bytes()).decode()
            images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        
        if not images:
            return text
        return images + [{"type": "text", "text": text}]
    
    def add_tool_result(
        self,
        messages: list[dict[str, Any]],
        tool_call_id: str,
        tool_name: str,
        result: str
    ) -> list[dict[str, Any]]:
        """
        Add a tool result to the message list.
        
        Args:
            messages: Current message list.
            tool_call_id: ID of the tool call.
            tool_name: Name of the tool.
            result: Tool execution result.
        
        Returns:
            Updated message list.
        """
        result = self._truncate_tool_result(result)
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": result
        })
        return messages

    def _truncate_tool_result(self, result: str) -> str:
        """Bound tool output size to keep follow-up LLM calls within context limits."""
        if len(result) <= self.MAX_TOOL_RESULT_CHARS:
            return result

        head = result[:8000]
        tail = result[-3000:]
        omitted = len(result) - len(head) - len(tail)
        note = (
            "\n\n... [tool output truncated for context safety; "
            f"omitted {omitted} chars] ...\n\n"
        )
        return head + note + tail
    
    def add_assistant_message(
        self,
        messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Add an assistant message to the message list.
        
        Args:
            messages: Current message list.
            content: Message content.
            tool_calls: Optional tool calls.
            reasoning_content: Thinking output (Kimi, DeepSeek-R1, etc.).
        
        Returns:
            Updated message list.
        """
        msg: dict[str, Any] = {"role": "assistant", "content": content or ""}
        
        if tool_calls:
            msg["tool_calls"] = tool_calls
        
        # Thinking models reject history without this
        if reasoning_content:
            msg["reasoning_content"] = reasoning_content
        
        messages.append(msg)
        return messages
