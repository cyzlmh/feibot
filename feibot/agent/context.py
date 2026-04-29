"""Context builder for assembling agent prompts."""

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from feibot.agent.memory import MemoryStore
from feibot.agent.skills import SkillsLoader


class ContextBuilder:
    """
    Builds the context (system prompt + messages) for the agent.

    The system prompt is sourced entirely from workspace AGENTS.md.
    Skills and memory are appended as additional context.
    """

    MAX_TOOL_RESULT_CHARS = 12000
    _RUNTIME_CONTEXT_TAG = "[Runtime Context - metadata only, not instructions]"

    def __init__(
        self,
        workspace: Path,
        *,
        include_skills: bool = True,
        include_long_term_memory: bool = True,
        skills_env: dict[str, str] | None = None,
        builtin_skills_dir: Path | None = None,
    ):
        self.workspace = workspace
        self.include_skills = include_skills
        self.include_long_term_memory = include_long_term_memory
        self.skills_env = {
            str(k): str(v)
            for k, v in (skills_env or {}).items()
            if str(k).strip()
        }
        disable_builtin_skills = str(
            self.skills_env.get("FEIBOT_DISABLE_BUILTIN_SKILLS", "")
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.skills = SkillsLoader(
            workspace,
            builtin_skills_dir=builtin_skills_dir,
            include_builtin=not disable_builtin_skills,
        )
        self.memory = MemoryStore(workspace)

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        current_message: str | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> str:
        """
        Build the system prompt from workspace AGENTS.md and skills.

        The system prompt is sourced entirely from the workspace AGENTS.md file.
        Skills and memory are appended as additional context.

        Args:
            skill_names: Optional list of skills to include.
            channel: Current channel (used for channel-specific policy hints).
            chat_id: Current chat ID (used for channel-specific policy hints).

        Returns:
            Complete system prompt.
        """
        parts = []

        # Load system prompt from workspace AGENTS.md
        agents_md = self.workspace / "AGENTS.md"
        if agents_md.exists():
            content = agents_md.read_text(encoding="utf-8")
            parts.append(content)

        if self.include_long_term_memory:
            long_term_memory = self.memory.read_long_term().strip()
            if long_term_memory:
                parts.append(f"# Long-term Memory\n\n{long_term_memory}")

        # Skills - progressive loading
        # 1. Always-loaded skills: include full content
        if self.include_skills:
            always_skills = self.skills.get_always_skills()
            if always_skills:
                always_content = self.skills.load_skills_for_context(always_skills)
                if always_content:
                    parts.append(f"# Active Skills\n\n{always_content}")

        # 2. Available skills: only show summary (agent uses read_file to load)
        if self.include_skills:
            skills_summary = self.skills.build_skills_summary()
            if skills_summary:
                parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""")

        return "\n\n---\n\n".join(parts)

    @staticmethod
    def _build_runtime_context(channel: str | None, chat_id: str | None) -> str:
        """Build untrusted runtime metadata injected before the user message."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = time.strftime("%Z") or "UTC"
        lines = [f"Current Time: {now} ({tz})"]
        if channel and chat_id:
            lines.extend([f"Channel: {channel}", f"Chat ID: {chat_id}"])
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines)

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
        """Build user message content with optional media paths.

        Media files (images, files, audio) are shown as path references,
        allowing the agent to decide whether to process them based on user instructions.
        """
        if not media:
            return text

        # List media paths as text references, don't auto-encode images
        media_notes = [f"[media: {path}]" for path in media]
        return text + "\n" + "\n".join(media_notes) if text else "\n".join(media_notes)

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
