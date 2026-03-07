"""Subagent manager for background task execution."""

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from feibot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from feibot.agent.tools.registry import ToolRegistry
from feibot.agent.tools.shell import ExecTool
from feibot.agent.tools.web import WebFetchTool, WebSearchTool
from feibot.bus.events import InboundMessage
from feibot.bus.queue import MessageBus
from feibot.providers.base import LLMProvider


class SubagentManager:
    """
    Manage background subagents.

    Subagents run a lightweight tool-enabled loop and report results back to the
    main agent via a system inbound message.
    """

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        brave_api_key: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        restrict_to_workspace: bool = False,
        allowed_dirs: list[str] | None = None,
    ):
        from feibot.config.schema import ExecToolConfig

        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.restrict_to_workspace = restrict_to_workspace
        self.allowed_dirs = allowed_dirs or []
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._session_tasks: dict[str, set[str]] = {}

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
    ) -> str:
        """Spawn a background subagent and return an immediate ack message."""
        task_id = str(uuid.uuid4())[:8]
        display_label = (label or task[:30]).strip() or "background task"
        if label is None and len(task) > 30:
            display_label += "..."

        origin = {"channel": origin_channel, "chat_id": origin_chat_id}
        bg_task = asyncio.create_task(self._run_subagent(task_id, task, display_label, origin))
        self._running_tasks[task_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)

        def _cleanup(_: asyncio.Task) -> None:
            self._running_tasks.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    self._session_tasks.pop(session_key, None)

        bg_task.add_done_callback(_cleanup)

        logger.info("Spawned subagent [{}]: {}", task_id, display_label)
        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."

    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
    ) -> None:
        logger.info("Subagent [{}] starting task: {}", task_id, label)
        try:
            tools = ToolRegistry()
            if self.restrict_to_workspace:
                allowed_dir = self.workspace
                # Also include additional allowed directories
                for d in self.allowed_dirs:
                    allowed_dir = allowed_dir.parent  # Expand to include parent
                # For now, just use workspace when restricted
            else:
                allowed_dir = None
            tools.register(ReadFileTool(allowed_dir=allowed_dir))
            tools.register(WriteFileTool(allowed_dir=allowed_dir))
            tools.register(EditFileTool(allowed_dir=allowed_dir))
            tools.register(ListDirTool(allowed_dir=allowed_dir))
            tools.register(
                ExecTool(
                    working_dir=str(self.workspace),
                    timeout=self.exec_config.timeout,
                    restrict_to_workspace=self.restrict_to_workspace,
                    path_append=self.exec_config.path_append,
                )
            )
            tools.register(WebSearchTool(api_key=self.brave_api_key))
            tools.register(WebFetchTool())

            messages: list[dict[str, Any]] = [
                {"role": "system", "content": self._build_subagent_prompt()},
                {"role": "user", "content": task},
            ]
            final_result: str | None = None
            max_iterations = 15

            for iteration in range(1, max_iterations + 1):
                response = await self.provider.chat(
                    messages=messages,
                    tools=tools.get_definitions(),
                    model=self.model,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                )

                if response.has_tool_calls:
                    tool_call_dicts = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                            },
                        }
                        for tc in response.tool_calls
                    ]
                    messages.append(
                        {
                            "role": "assistant",
                            "content": response.content or "",
                            "tool_calls": tool_call_dicts,
                        }
                    )

                    for tool_call in response.tool_calls:
                        args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                        logger.debug(
                            "Subagent [{}] iter {} executing {}({})",
                            task_id,
                            iteration,
                            tool_call.name,
                            args_str[:200],
                        )
                        result = await tools.execute(tool_call.name, tool_call.arguments)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "name": tool_call.name,
                                "content": result,
                            }
                        )
                    continue

                final_result = response.content or ""
                break

            if final_result is None:
                final_result = "Task completed but no final response was generated."

            logger.info("Subagent [{}] completed successfully", task_id)
            await self._announce_result(task_id, label, task, final_result, origin, status="ok")
        except Exception as e:
            logger.error("Subagent [{}] failed: {}", task_id, e)
            await self._announce_result(task_id, label, task, f"Error: {e}", origin, status="error")

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
    ) -> None:
        status_text = "completed successfully" if status == "ok" else "failed"
        announce_content = f"""[Subagent '{label}' {status_text}]

Task: {task}

Result:
{result}

Summarize this naturally for the user. Keep it brief (1-2 sentences). Do not mention technical details like "subagent" or task IDs."""

        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
            timestamp=datetime.now(),
            metadata={"_suppress_progress": True, "_subagent_task_id": task_id, "_subagent_status": status},
        )
        await self.bus.publish_inbound(msg)
        logger.debug(
            "Subagent [{}] announced result to {}:{}",
            task_id,
            origin["channel"],
            origin["chat_id"],
        )

    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel all subagents for the given session. Returns number cancelled."""
        task_ids = list(self._session_tasks.get(session_key, set()))
        tasks = [
            self._running_tasks[task_id]
            for task_id in task_ids
            if task_id in self._running_tasks and not self._running_tasks[task_id].done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def _build_subagent_prompt(self) -> str:
        return f"""# Subagent

You are a background subagent spawned by the main agent to complete a focused task.

## Rules
1. Stay focused on the assigned task
2. Be concise and factual
3. Use tools when needed
4. Do not initiate side tasks
5. Do not try to message the user directly

## Available Capabilities
- Read/write/edit/list files
- Execute shell commands
- Search the web and fetch pages

## Workspace
{self.workspace}
"""

    def get_running_count(self) -> int:
        return len(self._running_tasks)
