"""Shell execution tool."""

import asyncio
import os
import re
import shlex
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal

from feibot.agent.exec_approval import ExecApprovalManager
from feibot.agent.tools.base import Tool

RiskLevel = Literal["confirm", "dangerous"]


class ExecTool(Tool):
    """Tool to execute shell commands."""

    _SAFE_DEVICE_PATHS = {
        "/dev/null",
        "/dev/stdin",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/tty",
    }
    _URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
    _WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\\\/]")
    _SHELL_SEGMENT_SPLIT_RE = re.compile(r"(?:&&|\|\||;)")
    _ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
    _APPROVAL_PENDING_PREFIX = "approval-pending:"

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        confirm_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        allowed_dirs: list[str] | None = None,
        path_append: str = "",
        approval_manager: ExecApprovalManager | None = None,
        approval_mode_resolver: Callable[[RiskLevel, str, str], str] | None = None,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.deny_patterns = deny_patterns or [
            # Critical command patterns (dangerous-risk approval).
            # Block disk utility commands, but avoid matching option flags like
            # "--format"/"--merge-output-format" used by tools such as yt-dlp.
            r"(?<!-)\b(format|mkfs(?:\.[\w-]+)?|diskpart)\b",
            r"\bdd\s+if=",                   # dd
            r">\s*/dev/sd",                  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",          # fork bomb
            r"(?<![\w./-])rm\s+-[^\n]*\s+/\s*$",  # rm -rf /
        ]
        self.confirm_patterns = confirm_patterns or [
            # Non-critical but risky command patterns (require approval).
            r"(?<![\w./-])(?:/bin/)?rm\b",  # any rm command token
            r"\bdel\s+/[fq]\b",          # del /f, del /q
            r"\brmdir\s+/s\b",           # rmdir /s
            r"\b(?:curl|wget)\b[^\n]*\|[^\n]*\b(?:sh|bash)\b",
            r"\bchmod\s+(?:-r\s+)?777\b",
            r"\b(?:sudo|su)\b",
            r"\b(?:chown|chmod|chgrp|useradd|userdel|groupadd|passwd|visudo|systemctl|service|init)\b",
            r"\b(?:curl|wget|nc|netcat|ncat|ssh|scp|rsync|ftp|sftp)\b",
            r"\bcat\s+(?:/etc/(?:passwd|shadow)|~(?:/|$)|\$home/|\$\{home\}/)",
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.allowed_dirs = [Path(d).expanduser().resolve() for d in (allowed_dirs or [])]
        self.path_append = path_append
        self.approval_manager = approval_manager
        self.approval_mode_resolver = approval_mode_resolver
        self._channel_ctx: ContextVar[str] = ContextVar("exec_default_channel", default="")
        self._chat_id_ctx: ContextVar[str] = ContextVar("exec_default_chat_id", default="")
        self._sender_id_ctx: ContextVar[str] = ContextVar("exec_default_sender_id", default="")
        self._session_key_ctx: ContextVar[str] = ContextVar("exec_default_session_key", default="")

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "Execute a shell command and return its output. Use with caution."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute"
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command"
                }
            },
            "required": ["command"]
        }

    def set_context(
        self,
        *,
        channel: str,
        chat_id: str,
        sender_id: str,
        session_key: str,
    ) -> None:
        """Set per-request routing context for approvals."""
        self._channel_ctx.set(channel)
        self._chat_id_ctx.set(chat_id)
        self._sender_id_ctx.set(sender_id)
        self._session_key_ctx.set(session_key)

    async def execute(self, command: str, working_dir: str | None = None, **kwargs: Any) -> str:
        approval_granted = bool(kwargs.get("_approval_granted"))
        cwd = working_dir or self.working_dir or os.getcwd()
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error
        risk_level = self._risk_level(command)
        approval_mode = self._resolve_approval_mode(risk_level)
        if not approval_granted and risk_level is not None and approval_mode != "none":
            if approval_mode == "unavailable":
                channel = self._channel_ctx.get() or "unknown"
                return (
                    "Error: Command requires approval, but no supported approval workflow "
                    f"is available for channel '{channel}'."
                )
            if not self.approval_manager or not self.approval_manager.enabled:
                return "Error: Command requires approval but approval workflow is disabled"
            request = self.approval_manager.create_request(
                command=command,
                working_dir=cwd,
                channel=self._channel_ctx.get(),
                chat_id=self._chat_id_ctx.get(),
                session_key=self._session_key_ctx.get(),
                requester_id=self._sender_id_ctx.get(),
                risk_level=risk_level,
            )
            return self._build_approval_pending_result(request.id)

        env = os.environ.copy()
        if self.path_append:
            env["PATH"] = env.get("PATH", "") + os.pathsep + self.path_append

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                return f"Error: Command timed out after {self.timeout} seconds"

            output_parts = []

            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))

            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            if process.returncode != 0:
                output_parts.append(f"\nExit code: {process.returncode}")

            result = "\n".join(output_parts) if output_parts else "(no output)"

            # Truncate very long output
            max_len = 10000
            if len(result) > max_len:
                result = result[:max_len] + f"\n... (truncated, {len(result) - max_len} more chars)"

            return result

        except Exception as e:
            return f"Error executing command: {str(e)}"

    def _should_request_approval(self, *, command: str, cwd: str) -> bool:
        if not self.approval_manager or not self.approval_manager.enabled:
            return False
        risk_level = self._risk_level(command)
        return risk_level is not None and self._resolve_approval_mode(risk_level) not in {
            "none",
            "unavailable",
        }

    def _resolve_approval_mode(self, risk_level: RiskLevel | None) -> str:
        if risk_level is None:
            return "none"
        if self.approval_mode_resolver is None:
            return "feishu_card"
        mode = str(
            self.approval_mode_resolver(
                risk_level,
                self._channel_ctx.get(),
                self._sender_id_ctx.get(),
            )
            or "feishu_card"
        ).strip().lower()
        if mode in {"none", "feishu_card", "sim_auth", "unavailable"}:
            return mode
        return "feishu_card"

    def _requires_confirmation(self, command: str) -> bool:
        lower = command.strip().lower()
        return any(re.search(pattern, lower) for pattern in self.confirm_patterns)

    def _is_dangerous(self, command: str) -> bool:
        lower = command.strip().lower()
        return any(re.search(pattern, lower) for pattern in self.deny_patterns)

    def _risk_level(self, command: str) -> RiskLevel | None:
        if self._is_dangerous(command):
            return "dangerous"
        if self._requires_confirmation(command):
            return "confirm"
        return None

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort non-approval safety guard (policy/path checks)."""
        cmd = command.strip()
        lower = cmd.lower()

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        if self.restrict_to_workspace:
            workspace_root = self._to_lexical_path(self.working_dir or cwd)
            target_cwd = self._to_lexical_path(cwd, base=workspace_root)
            if not self._is_within_workspace(target_cwd, workspace_root):
                return "Error: Command blocked by safety guard (working directory outside workspace)"
            segment_cwd = target_cwd
            for segment in self._split_shell_segments(cmd):
                next_cwd = self._derive_next_cwd(segment, segment_cwd)
                if next_cwd is not None:
                    if not self._is_within_workspace(next_cwd, workspace_root):
                        return "Error: Command blocked by safety guard (working directory outside workspace)"
                    segment_cwd = next_cwd
                for raw in self._extract_path_candidates(segment):
                    # Allow common pseudo-device paths used only for shell redirection.
                    normalized_raw = raw.strip()
                    if normalized_raw in self._SAFE_DEVICE_PATHS or normalized_raw.startswith("/dev/fd/"):
                        continue
                    if self._is_url_like(normalized_raw):
                        continue
                    p = self._to_lexical_path(normalized_raw, base=segment_cwd)
                    if not self._is_within_workspace(p, workspace_root):
                        return "Error: Command blocked by safety guard (path outside workspace)"

        return None

    @classmethod
    def _extract_path_candidates(cls, command: str) -> set[str]:
        candidates: set[str] = set()
        try:
            tokens = shlex.split(command, posix=True)
        except Exception:
            tokens = [tok for tok in re.split(r"\s+", command) if tok]

        for token in tokens:
            if not token:
                continue
            values = [token]
            if "=" in token:
                _, rhs = token.split("=", 1)
                if rhs:
                    values.append(rhs)

            for value in values:
                candidate = value.strip().strip(",;")
                if not candidate:
                    continue
                candidate = re.sub(r"^\d*[<>]+", "", candidate)
                if not candidate or cls._URL_SCHEME_RE.match(candidate):
                    continue
                lower = candidate.lower()
                looks_relative_path = (
                    candidate.startswith("./")
                    or candidate.startswith("../")
                    or candidate.startswith(".\\")
                    or candidate.startswith("..\\")
                    or "/" in candidate
                    or "\\" in candidate
                )
                if (
                    candidate.startswith("/")
                    or candidate.startswith("~")
                    or lower.startswith("$home/")
                    or lower.startswith("${home}/")
                    or cls._WINDOWS_ABS_RE.match(candidate)
                    or looks_relative_path
                ):
                    candidates.add(candidate)

        # Preserve legacy absolute path matching so commands like "ls /tmp"
        # are still captured even if shell splitting fails.
        win_paths = re.findall(r"[A-Za-z]:\\[^\\\"']+", command)
        posix_paths = re.findall(r"(?:^|[\s|>])(/[^\s\"'>]+)", command)
        candidates.update(win_paths)
        candidates.update(posix_paths)
        return candidates

    @classmethod
    def _split_shell_segments(cls, command: str) -> list[str]:
        segments = [part.strip() for part in cls._SHELL_SEGMENT_SPLIT_RE.split(command)]
        return [segment for segment in segments if segment]

    @classmethod
    def _safe_shell_split(cls, text: str) -> list[str]:
        try:
            return shlex.split(text, posix=True)
        except Exception:
            return [tok for tok in re.split(r"\s+", text) if tok]

    @classmethod
    def _derive_next_cwd(cls, segment: str, current_cwd: Path) -> Path | None:
        tokens = cls._safe_shell_split(segment)
        if not tokens:
            return None

        idx = 0
        while idx < len(tokens) and cls._ENV_ASSIGNMENT_RE.match(tokens[idx]):
            idx += 1
        if idx >= len(tokens):
            return None
        if tokens[idx] not in {"cd", "pushd"}:
            return None

        target = tokens[idx + 1] if idx + 1 < len(tokens) else "~"
        if target == "-":
            # "cd -" depends on shell state and is not deterministic here.
            return cls._to_lexical_path("~")
        return cls._to_lexical_path(target, base=current_cwd)

    @classmethod
    def _to_lexical_path(cls, raw: str, *, base: Path | None = None) -> Path:
        expanded = os.path.expanduser(os.path.expandvars(raw))
        if not os.path.isabs(expanded):
            base_dir = str(base) if base is not None else os.getcwd()
            expanded = os.path.join(base_dir, expanded)
        return Path(os.path.abspath(os.path.normpath(expanded)))

    def _is_within_workspace(self, path: Path, workspace_root: Path) -> bool:
        # Check workspace root
        if path == workspace_root or workspace_root in path.parents:
            return True
        # Check additional allowed directories
        for allowed_dir in self.allowed_dirs:
            if path == allowed_dir or allowed_dir in path.parents:
                return True
        return False

    @classmethod
    def _is_url_like(cls, raw: str) -> bool:
        return bool(cls._URL_SCHEME_RE.match(raw))

    @classmethod
    def _build_approval_pending_result(cls, approval_id: str) -> str:
        return f"{cls._APPROVAL_PENDING_PREFIX}{approval_id}"

    @classmethod
    def parse_approval_pending_id(cls, result: str | None) -> str | None:
        if not isinstance(result, str):
            return None
        text = result.strip()
        if not text.startswith(cls._APPROVAL_PENDING_PREFIX):
            return None
        approval_id = text[len(cls._APPROVAL_PENDING_PREFIX):].strip()
        return approval_id or None
