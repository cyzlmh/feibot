"""Shell execution tool."""

import asyncio
import os
import re
import shlex
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from feibot.agent.tools.base import Tool
from feibot.agent.tools.path_guard import combine_roots


class ExecTool(Tool):
    """Tool to execute shell commands."""

    _APPROVAL_PENDING_PREFIX = "approval-pending:"
    _SAFE_DEVICE_PATHS = {
        "/dev/null",
        "/dev/stdin",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/tty",
    }
    _WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\\\/]")
    _URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
    _SHELL_SEGMENT_SPLIT_RE = re.compile(r"(?:&&|\|\||;)")
    _ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
    _REMOTE_SPEC_RE = re.compile(r"^(?:[^@:/\s]+@)?(?P<host>\[[^\]]+\]|[^:/\s]+):")
    _WRITE_REDIRECT_RE = re.compile(r"(?<!<)(?:^|[\s(])\d*(?:>>?|>\|)\s*([^\s;&|)]+)")
    _OPTION_VALUE_FLAGS = {
        "ssh": {"-b", "-c", "-D", "-E", "-e", "-F", "-i", "-J", "-L", "-l", "-m", "-o", "-p", "-R", "-S", "-W", "-w"},
        "scp": {"-c", "-D", "-F", "-i", "-J", "-l", "-o", "-P", "-S", "-X"},
        "rsync": {"-e", "--rsh", "--rsync-path", "--password-file", "--exclude", "--include"},
    }

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        writable_dirs: list[str] | None = None,
        allowed_hosts: list[str] | None = None,
        path_append: str = "",
        injected_env: dict[str, str] | None = None,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.writable_dirs = combine_roots(writable_dirs)
        self.allowed_hosts = {
            self._normalize_host(host)
            for host in (allowed_hosts or [])
            if self._normalize_host(host)
        }
        self.path_append = path_append
        self.injected_env = {
            str(k): str(v)
            for k, v in (injected_env or {}).items()
            if str(k).strip()
        }

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "Execute a shell command and return its output."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command",
                },
            },
            "required": ["command"],
        }

    def set_context(
        self,
        *,
        channel: str,
        chat_id: str,
        sender_id: str,
        session_key: str,
    ) -> None:
        """Preserve the existing tool interface; exec no longer uses per-request context."""
        return None

    async def run_command(self, command: str, working_dir: str | None = None) -> tuple[str, int]:
        cwd = working_dir or self.working_dir or os.getcwd()
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error, 126

        env = os.environ.copy()
        if self.injected_env:
            env.update(self.injected_env)
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
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                return f"Error: Command timed out after {self.timeout} seconds", 124

            output_parts = []
            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))
            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            result = "\n".join(output_parts)
            return result, int(process.returncode or 0)
        except Exception as e:
            return f"Error executing command: {str(e)}", 1

    async def execute(self, command: str, working_dir: str | None = None, **kwargs: Any) -> str:
        result, returncode = await self.run_command(command, working_dir=working_dir)
        rendered = result or "(no output)"
        if returncode != 0:
            rendered = f"{rendered}\n\nExit code: {returncode}"

        try:
            max_len = 10000
            if len(rendered) > max_len:
                rendered = rendered[:max_len] + f"\n... (truncated, {len(rendered) - max_len} more chars)"
            return rendered
        except Exception:
            return rendered

    def _guard_command(self, command: str, cwd: str) -> str | None:
        segment_cwd = self._to_lexical_path(cwd)

        for segment in self._split_shell_segments(command):
            next_cwd = self._derive_next_cwd(segment, segment_cwd)
            if next_cwd is not None:
                segment_cwd = next_cwd

            host_error = self._guard_hosts(segment)
            if host_error:
                return host_error

            write_error = self._guard_writes(segment, segment_cwd)
            if write_error:
                return write_error

        return None

    def _guard_hosts(self, segment: str) -> str | None:
        if not self.allowed_hosts:
            return None

        for host in self._extract_remote_hosts(segment):
            if host not in self.allowed_hosts:
                joined = ", ".join(sorted(self.allowed_hosts))
                return f"Error: Remote host '{host}' is not in allowedHosts: {joined}"
        return None

    def _guard_writes(self, segment: str, cwd: Path) -> str | None:
        if not self.writable_dirs:
            return None

        # Skip write checks for SSH commands - remote commands execute on the target host
        tokens = self._safe_shell_split(segment)
        cmd, _ = self._command_parts(tokens)
        if cmd == "ssh":
            return None

        for target in self._extract_write_targets(segment):
            error = self._check_writable_target(target, cwd)
            if error:
                return error
        return None

    def _check_writable_target(self, raw: str, cwd: Path) -> str | None:
        candidate = raw.strip().strip(",;")
        if not candidate:
            return None
        candidate = re.sub(r"^\d*(?:>>?|>\|)", "", candidate).strip()
        if not candidate or candidate == "-":
            return None
        if candidate in self._SAFE_DEVICE_PATHS or candidate.startswith("/dev/fd/"):
            return None
        if self._URL_RE.match(candidate):
            return None

        target_path = self._to_lexical_path(candidate, base=cwd)
        if self._is_within_writable_dirs(target_path):
            return None

        joined = ", ".join(str(item) for item in self.writable_dirs)
        return f"Error: Write target {candidate} is outside writableDirs: {joined}"

    def _extract_write_targets(self, segment: str) -> list[str]:
        targets: list[str] = []
        targets.extend(match.group(1) for match in self._WRITE_REDIRECT_RE.finditer(segment))

        tokens = self._safe_shell_split(segment)
        cmd, args = self._command_parts(tokens)
        if not cmd:
            return targets

        if cmd in {"rm", "rmdir", "unlink", "touch", "mkdir", "install", "chmod", "chown", "chgrp", "truncate"}:
            targets.extend(arg for arg in args if arg and not arg.startswith("-"))
        elif cmd in {"cp", "mv", "ln"}:
            dest = self._extract_destination_arg(args)
            if dest:
                targets.append(dest)
        elif cmd == "tee":
            targets.extend(arg for arg in args if arg and not arg.startswith("-") and arg != "-")
        elif cmd == "dd":
            for arg in args:
                if arg.startswith("of="):
                    targets.append(arg.split("=", 1)[1])
        elif cmd == "sed":
            targets.extend(self._extract_sed_in_place_targets(args))

        return targets

    def _extract_remote_hosts(self, segment: str) -> list[str]:
        tokens = self._safe_shell_split(segment)
        cmd, args = self._command_parts(tokens)
        if not cmd or cmd not in {"ssh", "scp", "rsync"}:
            return []

        hosts: list[str] = []
        skip_next = False
        option_value_flags = self._OPTION_VALUE_FLAGS.get(cmd, set())
        positional: list[str] = []

        for arg in args:
            if skip_next:
                skip_next = False
                continue
            if arg in option_value_flags:
                skip_next = True
                continue
            if arg.startswith("--") and "=" in arg:
                continue
            if arg.startswith("-"):
                continue
            positional.append(arg)

        if cmd == "ssh":
            if positional:
                host = self._extract_ssh_host(positional[0])
                if host:
                    hosts.append(host)
            return hosts

        for arg in positional:
            host = self._extract_remote_spec_host(arg)
            if host:
                hosts.append(host)
        return hosts

    def _extract_ssh_host(self, raw: str) -> str | None:
        if self._URL_RE.match(raw):
            parsed = urlparse(raw)
            return self._normalize_host(parsed.hostname)

        host = raw.rsplit("@", 1)[-1]
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        return self._normalize_host(host)

    def _extract_remote_spec_host(self, raw: str) -> str | None:
        if self._URL_RE.match(raw):
            parsed = urlparse(raw)
            return self._normalize_host(parsed.hostname)

        match = self._REMOTE_SPEC_RE.match(raw)
        if not match:
            return None

        host = match.group("host")
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        return self._normalize_host(host)

    def _extract_sed_in_place_targets(self, args: list[str]) -> list[str]:
        in_place = False
        skip_next = False
        positional: list[str] = []

        for arg in args:
            if skip_next:
                skip_next = False
                continue
            if arg in {"-e", "-f"}:
                skip_next = True
                continue
            if arg in {"-i", "--in-place"} or arg.startswith("-i"):
                in_place = True
                continue
            if arg.startswith("-"):
                continue
            positional.append(arg)

        if not in_place or not positional:
            return []
        if len(positional) == 1:
            return positional
        return positional[1:]

    def _extract_destination_arg(self, args: list[str]) -> str | None:
        positional: list[str] = []
        skip_next = False

        for arg in args:
            if skip_next:
                skip_next = False
                continue
            if arg in {"-t", "--target-directory"}:
                skip_next = True
                continue
            if arg.startswith("--target-directory="):
                return arg.split("=", 1)[1]
            if arg.startswith("-"):
                continue
            positional.append(arg)

        if len(positional) >= 2:
            return positional[-1]
        return None

    def _command_parts(self, tokens: list[str]) -> tuple[str, list[str]]:
        idx = 0
        while idx < len(tokens) and self._ENV_ASSIGNMENT_RE.match(tokens[idx]):
            idx += 1
        if idx >= len(tokens):
            return "", []
        return tokens[idx], tokens[idx + 1 :]

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
    def parse_approval_pending_id(cls, result: str | None) -> str | None:
        """Compatibility shim for mixed-version runtimes after approval removal."""
        if not isinstance(result, str):
            return None
        text = result.strip()
        if not text.startswith(cls._APPROVAL_PENDING_PREFIX):
            return None
        approval_id = text[len(cls._APPROVAL_PENDING_PREFIX):].strip()
        return approval_id or None

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
            return cls._to_lexical_path("~")
        return cls._to_lexical_path(target, base=current_cwd)

    @classmethod
    def _to_lexical_path(cls, raw: str, *, base: Path | None = None) -> Path:
        expanded = os.path.expanduser(os.path.expandvars(raw))
        if not os.path.isabs(expanded):
            base_dir = str(base) if base is not None else os.getcwd()
            expanded = os.path.join(base_dir, expanded)
        return Path(os.path.abspath(os.path.normpath(expanded)))

    def _is_within_writable_dirs(self, path: Path) -> bool:
        for root in self.writable_dirs:
            if path == root or root in path.parents:
                return True
        return False

    @staticmethod
    def _normalize_host(host: str | None) -> str:
        text = str(host or "").strip().lower()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        return text
