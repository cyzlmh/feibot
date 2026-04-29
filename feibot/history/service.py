"""Nightly session history sync and memory recommendation service."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from feibot.agent.memory import MemoryStore
from feibot.providers.base import LLMProvider
from feibot.session.manager import Session, SessionManager

_HISTORY_SYNC_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "sync_session_history",
            "description": "Summarize one session and propose optional long-term memory candidates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "A concise summary of the session's important events and decisions.",
                    },
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Short search keywords for this session.",
                    },
                    "memory_candidates": {
                        "type": "array",
                        "description": (
                            "Durable candidate facts that might belong in MEMORY.md. "
                            "Return an empty list when there is nothing durable enough."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "candidate": {"type": "string"},
                                "reason": {"type": "string"},
                            },
                            "required": ["candidate", "reason"],
                        },
                    },
                },
                "required": ["summary", "keywords", "memory_candidates"],
            },
        },
    }
]


@dataclass
class MemoryCandidate:
    candidate: str
    reason: str


@dataclass
class SessionHistorySummary:
    summary: str
    keywords: list[str]
    memory_candidates: list[MemoryCandidate]


class HistorySyncService:
    """Build a nightly searchable history index from full session logs."""

    STATE_FILE = "history_state.json"

    def __init__(
        self,
        workspace: Path,
        session_manager: SessionManager,
        provider: LLMProvider,
        model: str,
    ):
        self.workspace = workspace
        self.sessions = session_manager
        self.provider = provider
        self.model = model
        self.memory = MemoryStore(workspace)
        self.state_path = self.memory.memory_dir / self.STATE_FILE

    def _load_state(self) -> dict[str, dict[str, Any]]:
        if not self.state_path.exists():
            return {}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("History sync: failed to load state: {}", e)
            return {}
        sessions = raw.get("sessions") if isinstance(raw, dict) else {}
        if not isinstance(sessions, dict):
            return {}
        return {str(k): dict(v) for k, v in sessions.items() if isinstance(v, dict)}

    def _save_state(self, state: dict[str, dict[str, Any]]) -> None:
        payload = {"version": 1, "sessions": state}
        self.state_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def _session_marker(session_id: str, boundary: str) -> str:
        return f"<!-- session:{session_id}:{boundary} -->"

    def _upsert_history_block(
        self,
        content: str,
        *,
        session: Session,
        summary: SessionHistorySummary,
    ) -> str:
        start = self._session_marker(session.session_id, "start")
        end = self._session_marker(session.session_id, "end")
        block = "\n".join(
            [
                start,
                f"## {session.created_at:%Y-%m-%d} | {session.key}",
                f"- session_id: {session.session_id}",
                f"- started_at: {session.created_at.isoformat(timespec='seconds')}",
                f"- updated_at: {session.updated_at.isoformat(timespec='seconds')}",
                f"- keywords: {', '.join(summary.keywords) if summary.keywords else '(none)'}",
                f"- summary: {summary.summary.strip()}",
                end,
                "",
            ]
        )
        pattern = re.compile(
            rf"{re.escape(start)}.*?{re.escape(end)}\n?",
            flags=re.DOTALL,
        )
        if pattern.search(content):
            content = pattern.sub(block, content)
            return content.strip() + "\n"

        if content and not content.endswith("\n"):
            content += "\n"
        content += block
        return content

    @staticmethod
    def _clip(text: str, max_chars: int) -> str:
        stripped = text.strip().replace("\n", " ")
        if len(stripped) <= max_chars:
            return stripped
        return stripped[: max_chars - 3].rstrip() + "..."

    def _render_session_transcript(self, session: Session) -> str:
        lines: list[str] = []
        for msg in session.messages:
            role = str(msg.get("role") or "").lower()
            content = str(msg.get("content") or "").strip()
            if role == "tool":
                tool_name = str(msg.get("name") or "tool")
                if not content:
                    continue
                lines.append(
                    f"[{str(msg.get('timestamp') or '')[:16]}] TOOL({tool_name}): "
                    f"{self._clip(content, 240)}"
                )
                continue
            if role == "assistant" and msg.get("tool_calls") and not content:
                call_names = [
                    tc.get("function", {}).get("name", "tool")
                    for tc in msg.get("tool_calls", [])
                    if isinstance(tc, dict)
                ]
                content = f"[tool calls] {', '.join(call_names)}" if call_names else "[tool calls]"
            if not content:
                continue
            lines.append(
                f"[{str(msg.get('timestamp') or '')[:16]}] {role.upper()}: {self._clip(content, 1200)}"
            )
        return "\n".join(lines)

    async def _summarize_session(self, session: Session) -> SessionHistorySummary:
        transcript = self._render_session_transcript(session)
        current_memory = self.memory.read_long_term().strip() or "(empty)"
        prompt = f"""Summarize this session for a searchable history index.

Rules:
- Focus on what happened in this specific session.
- Keep the summary concise but concrete.
- Keywords should be short search terms, lower-case when possible.
- Only propose memory candidates when they are durable, global, and worth loading into every future prompt.
- Do not propose transient bugs, one-off tasks, or session-local details as memory candidates.
- The user must approve any memory candidate before it can be added to MEMORY.md.

## Current approved MEMORY.md
{current_memory}

## Session metadata
- session_id: {session.session_id}
- key: {session.key}
- started_at: {session.created_at.isoformat(timespec='seconds')}
- updated_at: {session.updated_at.isoformat(timespec='seconds')}

## Session transcript
{transcript or "(empty)"}
"""

        try:
            response = await self.provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a history indexing agent. Call the sync_session_history tool "
                            "for each session summary."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                tools=_HISTORY_SYNC_TOOL,
                model=self.model,
                temperature=0,
            )
            if response.has_tool_calls:
                args = response.tool_calls[0].arguments if response.tool_calls else {}
                if isinstance(args, dict):
                    keywords_raw = args.get("keywords") or []
                    candidates_raw = args.get("memory_candidates") or []
                    keywords = [
                        str(item).strip()
                        for item in keywords_raw
                        if str(item).strip()
                    ]
                    candidates: list[MemoryCandidate] = []
                    for item in candidates_raw:
                        if not isinstance(item, dict):
                            continue
                        candidate = str(item.get("candidate") or "").strip()
                        reason = str(item.get("reason") or "").strip()
                        if candidate and reason:
                            candidates.append(MemoryCandidate(candidate=candidate, reason=reason))
                    summary_text = str(args.get("summary") or "").strip()
                    if summary_text:
                        return SessionHistorySummary(
                            summary=summary_text,
                            keywords=keywords[:8],
                            memory_candidates=candidates,
                        )
        except Exception as e:
            logger.warning("History sync: failed to summarize {}: {}", session.session_id, e)

        return SessionHistorySummary(
            summary=self._fallback_summary(session),
            keywords=self._fallback_keywords(session),
            memory_candidates=[],
        )

    @staticmethod
    def _fallback_summary(session: Session) -> str:
        if not session.messages:
            return "No recorded messages in this session."
        first = next(
            (str(msg.get("content") or "").strip() for msg in session.messages if str(msg.get("content") or "").strip()),
            "",
        )
        last = next(
            (
                str(msg.get("content") or "").strip()
                for msg in reversed(session.messages)
                if str(msg.get("content") or "").strip()
            ),
            "",
        )
        if first and last and first != last:
            return f"Session started with: {HistorySyncService._clip(first, 180)} Final outcome: {HistorySyncService._clip(last, 220)}"
        if first:
            return f"Session content: {HistorySyncService._clip(first, 220)}"
        return "Session contained only non-text tool activity."

    @staticmethod
    def _fallback_keywords(session: Session) -> list[str]:
        raw = f"{session.key} {session.session_id}"
        seen: list[str] = []
        for token in re.findall(r"[A-Za-z0-9_/-]{3,}", raw):
            if token.lower() not in seen:
                seen.append(token.lower())
        return seen[:6]

    @staticmethod
    def _is_dirty(session: Session, state: dict[str, dict[str, Any]]) -> bool:
        if not session.session_id or not session.messages:
            return False
        previous = state.get(session.session_id)
        if not previous:
            return True
        return (
            str(previous.get("updated_at") or "") != session.updated_at.isoformat()
            or int(previous.get("message_count") or 0) != len(session.messages)
        )

    def _render_review(self, items: list[tuple[Session, MemoryCandidate]]) -> str:
        lines = [
            "# Reflection Notes",
            "",
            "Possible durable memory items observed during nightly reflection.",
            "These are notes only. Do not merge them into MEMORY.md without explicit user approval.",
            "",
        ]
        if not items:
            lines.append("No possible durable memory items from the latest reflection.")
            return "\n".join(lines).rstrip() + "\n"

        current_session = None
        for session, candidate in items:
            if current_session != session.session_id:
                if current_session is not None:
                    lines.append("")
                current_session = session.session_id
                lines.extend(
                    [
                        f"## {session.created_at:%Y-%m-%d} | {session.key}",
                        f"- session_id: {session.session_id}",
                    ]
                )
            lines.append(f"- possible memory item: {candidate.candidate}")
            lines.append(f"  reason: {candidate.reason}")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _render_notification(
        updated: list[tuple[Session, SessionHistorySummary]],
        review_items: list[tuple[Session, MemoryCandidate]],
    ) -> str:
        if not updated:
            return "Daily reflection report: no updated sessions since the last run."

        lines = [
            "Daily reflection report",
            "",
            f"- Updated {len(updated)} session summaries in `memory/HISTORY.md`.",
            "No changes were made to `memory/MEMORY.md`.",
        ]
        lines.append("")
        lines.append("Yesterday / since-last-run takeaways:")
        for session, summary in updated[:5]:
            lines.append(f"- {session.key}: {summary.summary}")
        if len(updated) > 5:
            lines.append(f"- ... and {len(updated) - 5} more session summaries in `memory/HISTORY.md`.")

        if not review_items:
            lines.append("")
            lines.append("No durable memory suggestions came out of this reflection.")
            lines.append("If you want to update memory, tell me naturally in chat.")
            return "\n".join(lines)

        lines.append("")
        lines.append("Possible durable memory items to consider:")
        for session, candidate in review_items:
            lines.append(f"- [{session.session_id}] {candidate.candidate}")
            lines.append(f"  reason: {candidate.reason}")
        lines.append("")
        lines.append("If you want any of these added to or removed from `memory/MEMORY.md`, tell me in natural language.")
        return "\n".join(lines)

    async def run(self) -> str | None:
        state = self._load_state()
        sessions = [s for s in self.sessions.iter_sessions() if self._is_dirty(s, state)]
        sessions.sort(key=lambda s: s.updated_at)

        if not sessions:
            self.memory.write_review(self._render_review([]))
            return "Daily reflection report: no updated sessions since the last run."

        history_content = self.memory.read_history()
        updated: list[tuple[Session, SessionHistorySummary]] = []
        review_items: list[tuple[Session, MemoryCandidate]] = []

        for session in sessions:
            summary = await self._summarize_session(session)
            history_content = self._upsert_history_block(
                history_content,
                session=session,
                summary=summary,
            )
            updated.append((session, summary))
            for candidate in summary.memory_candidates:
                review_items.append((session, candidate))
            state[session.session_id] = {
                "updated_at": session.updated_at.isoformat(),
                "message_count": len(session.messages),
            }

        self.memory.write_history(history_content.rstrip() + "\n")
        self.memory.write_review(self._render_review(review_items))
        self._save_state(state)
        return self._render_notification(updated, review_items)
