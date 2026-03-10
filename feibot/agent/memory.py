"""Memory system for persistent agent memory."""

import re
from pathlib import Path

from feibot.utils.helpers import ensure_dir


class MemoryStore:
    """Two-layer memory: MEMORY.md (long-term facts) + HISTORY.md (grep-searchable log)."""

    def __init__(self, workspace: Path):
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"
        self.review_file = self.memory_dir / "REVIEW.md"

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def read_history(self) -> str:
        if self.history_file.exists():
            return self.history_file.read_text(encoding="utf-8")
        return ""

    def write_history(self, content: str) -> None:
        self.history_file.write_text(content, encoding="utf-8")

    def read_review(self) -> str:
        if self.review_file.exists():
            return self.review_file.read_text(encoding="utf-8")
        return ""

    def write_review(self, content: str) -> None:
        self.review_file.write_text(content, encoding="utf-8")

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        tokens = set()
        for t in re.findall(r"[A-Za-z0-9_]{3,}|[\u4e00-\u9fff]+", text):
            tokens.add(t.lower())
        return tokens

    @staticmethod
    def _split_blocks(content: str) -> list[str]:
        """Split markdown memory into semantically coarse blocks."""
        lines = content.splitlines()
        blocks: list[str] = []
        current: list[str] = []

        for line in lines:
            if line.strip().startswith("#") and current:
                blocks.append("\n".join(current).strip())
                current = [line]
                continue
            current.append(line)

        if current:
            blocks.append("\n".join(current).strip())

        normalized: list[str] = []
        for block in blocks:
            for sub in re.split(r"\n\s*\n", block):
                sub = sub.strip()
                if sub:
                    normalized.append(sub)
        return normalized

    @staticmethod
    def _normalize_query(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    def get_memory_context(
        self,
        query: str | None = None,
        *,
        max_blocks: int = 6,
        max_chars: int = 3500,
    ) -> str:
        """
        Build memory context for the current turn.

        If query is provided, select top relevant memory blocks to reduce
        prompt noise. When nothing matches, return no memory instead of
        injecting an unrelated excerpt.
        """
        long_term = self.read_long_term().strip()
        if not long_term:
            return ""

        if not query or not query.strip():
            excerpt = long_term[:max_chars].strip()
            return f"## Long-term Memory\n{excerpt}"

        blocks = self._split_blocks(long_term)
        if not blocks:
            excerpt = long_term[:max_chars].strip()
            return f"## Long-term Memory\n{excerpt}"

        normalized_query = self._normalize_query(query)
        query_tokens = self._tokenize(normalized_query)
        if not query_tokens:
            return ""

        query_lower = normalized_query.lower()
        scored: list[tuple[int, int, str]] = []

        for idx, block in enumerate(blocks):
            block_tokens = self._tokenize(block)
            overlap = len(query_tokens & block_tokens)
            phrase_bonus = 2 if len(query_lower) >= 4 and query_lower in block.lower() else 0
            score = overlap + phrase_bonus
            if score > 0:
                scored.append((score, idx, block))

        if not scored:
            return ""

        # Keep strongest matches, then restore original order for readability.
        top = sorted(scored, key=lambda x: (-x[0], x[1]))[:max_blocks]
        selected = [item[2] for item in sorted(top, key=lambda x: x[1])]

        trimmed: list[str] = []
        used = 0
        for block in selected:
            remaining = max_chars - used
            if remaining <= 0:
                break
            part = block if len(block) <= remaining else block[:remaining].rstrip()
            trimmed.append(part)
            used += len(part) + 2

        return "## Long-term Memory (relevant excerpts)\n" + "\n\n".join(trimmed)
