"""Search-oriented tools to reduce unnecessary shell exploration."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from feibot.agent.tools.base import Tool


def _resolve_workspace_path(
    path: str | None,
    workspace_dir: Path,
    allowed_dir: Path | None = None,
) -> Path:
    """Resolve a root path and enforce workspace-only access."""
    workspace_root = workspace_dir.expanduser().resolve()
    raw = (path or str(workspace_root)).strip()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    resolved = candidate.resolve()

    try:
        resolved.relative_to(workspace_root)
    except ValueError as e:
        raise PermissionError(
            f"Path {resolved} is outside workspace directory {workspace_root}"
        ) from e

    if allowed_dir:
        base = allowed_dir.expanduser().resolve()
        try:
            resolved.relative_to(base)
        except ValueError as e:
            raise PermissionError(f"Path {resolved} is outside allowed directory {base}") from e

    return resolved


class FindFileTool(Tool):
    """Find files by name/path match within a directory tree."""

    def __init__(self, base_dir: Path, allowed_dir: Path | None = None):
        self._base_dir = base_dir
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "find_file"

    @property
    def description(self) -> str:
        return (
            "Find files under a directory by filename/path substring match. "
            "Use this before exec for code/file discovery."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Filename or path fragment to match."},
                "root": {"type": "string", "description": "Search root directory. Defaults to workspace root."},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["query"],
        }

    async def execute(
        self,
        query: str,
        root: str | None = None,
        max_results: int = 100,
        **kwargs: Any,
    ) -> str:
        try:
            root_path = _resolve_workspace_path(root, self._base_dir, self._allowed_dir)
            if not root_path.exists() or not root_path.is_dir():
                return f"Error: Directory not found: {root_path}"

            q = query.strip()
            if not q:
                return "Error: query cannot be empty"

            fd_bin = shutil.which("fd") or shutil.which("fdfind")
            if not fd_bin:
                return "Error: fd command not found (install fd/fdfind)"

            args = [
                "--hidden",
                "--type",
                "f",
                "--absolute-path",
                "--full-path",
                "--fixed-strings",
                "--ignore-case",
                "--max-results",
                str(max_results),
                q,
                str(root_path),
            ]
            proc = await asyncio.create_subprocess_exec(
                fd_bin,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            exit_code = proc.returncode or 0

            if exit_code not in (0, 1):
                error_text = stderr.decode("utf-8", errors="replace").strip() or f"fd exited with code {exit_code}"
                return f"Error finding files: {error_text}"

            results = [line.strip() for line in stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]

            if not results:
                return f"No files found for query '{query}' under {root_path}"
            return "\n".join(results)
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error finding files: {e}"


class GrepTextTool(Tool):
    """Search text patterns in files using ripgrep."""

    def __init__(self, base_dir: Path, allowed_dir: Path | None = None):
        self._base_dir = base_dir
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "grep_text"

    @property
    def description(self) -> str:
        return (
            "Search text in files and return matching file:line results. "
            "Prefer this over exec+grep for source code exploration."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern or plain text to search."},
                "root": {"type": "string", "description": "Search root directory. Defaults to workspace root."},
                "file_glob": {
                    "type": "string",
                    "description": "Filename glob filter (e.g. '*.py', '*.md'). Defaults to '*'.",
                },
                "regex": {
                    "type": "boolean",
                    "description": "Treat pattern as regex. Defaults to true.",
                },
                "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "required": ["pattern"],
        }

    async def execute(
        self,
        pattern: str,
        root: str | None = None,
        file_glob: str = "*",
        regex: bool = True,
        max_results: int = 200,
        **kwargs: Any,
    ) -> str:
        try:
            root_path = _resolve_workspace_path(root, self._base_dir, self._allowed_dir)
            if not root_path.exists() or not root_path.is_dir():
                return f"Error: Directory not found: {root_path}"

            rg_bin = shutil.which("rg")
            if not rg_bin:
                return "Error: rg command not found (install ripgrep)"

            args = [
                "--line-number",
                "--with-filename",
                "--color=never",
                "--hidden",
                "--no-messages",
            ]
            if file_glob and file_glob != "*":
                args.extend(["--glob", file_glob])
            if not regex:
                args.append("--fixed-strings")
            args.extend([pattern, str(root_path)])

            proc = await asyncio.create_subprocess_exec(
                rg_bin,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            out: list[str] = []
            stdout = proc.stdout
            if stdout is None:
                return "Error grepping text: failed to read rg output"

            while True:
                line = await stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                if not text:
                    continue
                out.append(text)
                if len(out) >= max_results:
                    if proc.returncode is None:
                        proc.kill()
                    break

            stderr_bytes = await proc.stderr.read() if proc.stderr else b""
            await proc.wait()
            exit_code = proc.returncode or 0

            if exit_code not in (0, 1) and not (len(out) >= max_results):
                error_text = stderr_bytes.decode("utf-8", errors="replace").strip() or f"rg exited with code {exit_code}"
                return f"Error grepping text: {error_text}"

            if not out:
                return f"No matches for pattern '{pattern}' under {root_path} (glob='{file_glob}')"
            return "\n".join(out)
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error grepping text: {e}"
