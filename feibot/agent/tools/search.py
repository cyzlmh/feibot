"""Search-oriented tools to reduce unnecessary shell exploration."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from feibot.agent.tools.base import Tool


def _resolve_path(path: str, allowed_dir: Path | None = None) -> Path:
    """Resolve path and optionally enforce directory restriction."""
    resolved = Path(path).expanduser().resolve()
    if allowed_dir:
        base = allowed_dir.expanduser().resolve()
        try:
            resolved.relative_to(base)
        except ValueError as e:
            raise PermissionError(f"Path {path} is outside allowed directory {allowed_dir}") from e
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
            root_path = _resolve_path(root or str(self._base_dir), self._allowed_dir)
            if not root_path.exists() or not root_path.is_dir():
                return f"Error: Directory not found: {root_path}"

            q = query.lower().strip()
            results: list[str] = []
            for p in root_path.rglob("*"):
                if not p.is_file():
                    continue
                rel = str(p.relative_to(root_path))
                if q in p.name.lower() or q in rel.lower():
                    results.append(str(p))
                    if len(results) >= max_results:
                        break

            if not results:
                return f"No files found for query '{query}' under {root_path}"
            return "\n".join(results)
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error finding files: {e}"


class GrepTextTool(Tool):
    """Search text patterns in files without shelling out."""

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
            root_path = _resolve_path(root or str(self._base_dir), self._allowed_dir)
            if not root_path.exists() or not root_path.is_dir():
                return f"Error: Directory not found: {root_path}"

            matcher = re.compile(pattern) if regex else None
            out: list[str] = []
            files_scanned = 0

            for p in root_path.rglob("*"):
                if not p.is_file():
                    continue
                if file_glob and not fnmatch.fnmatch(p.name, file_glob):
                    continue
                files_scanned += 1

                try:
                    with p.open("r", encoding="utf-8", errors="ignore") as fh:
                        for i, line in enumerate(fh, start=1):
                            line_body = line.rstrip("\n")
                            hit = bool(matcher.search(line_body)) if matcher else (pattern in line_body)
                            if hit:
                                out.append(f"{p}:{i}: {line_body}")
                                if len(out) >= max_results:
                                    return "\n".join(out)
                except Exception:
                    continue

            if not out:
                return (
                    f"No matches for pattern '{pattern}' under {root_path} "
                    f"(glob='{file_glob}', files_scanned={files_scanned})"
                )
            return "\n".join(out)
        except re.error as e:
            return f"Error: Invalid regex pattern: {e}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error grepping text: {e}"
