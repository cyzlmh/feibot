"""Session management for conversation history."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from feibot.utils.helpers import ensure_dir, safe_filename


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _state_fingerprint(
    *,
    session_id: str,
    key: str,
    created_at: datetime,
    updated_at: datetime,
    metadata: dict[str, Any],
    last_consolidated: int,
) -> str:
    return json.dumps(
        {
            "session_id": session_id,
            "key": key,
            "created_at": created_at.isoformat(),
            "updated_at": updated_at.isoformat(),
            "metadata": metadata,
            "last_consolidated": last_consolidated,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


@dataclass
class Session:
    """
    A conversation session.

    Session files are append-only JSONL archives. Each file keeps the full
    turn history for one logical conversation session, and a small active index
    maps ``channel:chat_id`` to the current live session file.
    """

    key: str  # channel:chat_id
    session_id: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # kept for backward-compatible file parsing
    storage_path: str | None = None
    _saved_message_count: int = field(default=0, repr=False)
    _saved_state_fingerprint: str = field(default="", repr=False)

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs,
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    @staticmethod
    def _is_history_slice_coherent(messages: list[dict[str, Any]]) -> bool:
        """
        Validate tool-call adjacency constraints required by strict providers.

        This guards against truncation that keeps tool results but drops the
        assistant message that introduced the corresponding tool_call_id.
        """
        declared_ids: set[str] = set()
        pending_ids: set[str] | None = None

        for m in messages:
            role = m.get("role")

            if role == "assistant" and m.get("tool_calls"):
                if pending_ids:
                    return False
                ids = {
                    str(tc.get("id"))
                    for tc in (m.get("tool_calls") or [])
                    if isinstance(tc, dict) and tc.get("id")
                }
                if not ids:
                    return False
                declared_ids.update(ids)
                pending_ids = set(ids)
                continue

            if role == "tool":
                tool_call_id = str(m.get("tool_call_id") or "")
                if not tool_call_id:
                    return False
                if tool_call_id not in declared_ids:
                    return False
                if not pending_ids or tool_call_id not in pending_ids:
                    return False
                pending_ids.remove(tool_call_id)
                if not pending_ids:
                    pending_ids = None
                continue

            if pending_ids:
                return False

        return not pending_ids

    @classmethod
    def _trim_to_coherent_history(cls, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop the smallest broken prefix caused by truncation."""
        if cls._is_history_slice_coherent(messages):
            return messages

        for start in range(1, len(messages)):
            candidate = messages[start:]
            if cls._is_history_slice_coherent(candidate):
                return candidate
        return []

    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]:
        """Get recent messages in LLM format, preserving key tool metadata."""
        recent = self.messages[-max_messages:]
        recent = self._trim_to_coherent_history(recent)

        out: list[dict[str, Any]] = []
        for m in recent:
            entry: dict[str, Any] = {"role": m["role"], "content": m.get("content", "")}
            for k in ("tool_calls", "tool_call_id", "name", "reasoning_content"):
                if k in m:
                    entry[k] = m[k]
            out.append(entry)
        return out

    def clear(self) -> None:
        """Clear in-memory messages for the current session object."""
        self.messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()
        self._saved_message_count = 0
        self._saved_state_fingerprint = ""


class SessionManager:
    """
    Manages conversation sessions.

    Active session routing is stored in ``_active_sessions.json`` while each
    session archive lives in a dated append-only JSONL file.
    """

    ACTIVE_INDEX_FILE = "_active_sessions.json"

    def __init__(self, sessions_dir: Path):
        self.sessions_dir = ensure_dir(sessions_dir)
        self._cache: dict[str, Session] = {}
        self._active_index: dict[str, str] | None = None

    @property
    def active_index_path(self) -> Path:
        return self.sessions_dir / self.ACTIVE_INDEX_FILE

    def _load_active_index(self) -> dict[str, str]:
        if self._active_index is not None:
            return self._active_index

        if not self.active_index_path.exists():
            self._active_index = {}
            return self._active_index

        try:
            data = json.loads(self.active_index_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Failed to load session active index: {}", e)
            self._active_index = {}
            return self._active_index

        active = data.get("active") if isinstance(data, dict) else {}
        if not isinstance(active, dict):
            active = {}
        self._active_index = {str(k): str(v) for k, v in active.items() if k and v}
        return self._active_index

    def _save_active_index(self) -> None:
        active = self._load_active_index()
        payload = {"version": 1, "active": active}
        self.active_index_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _legacy_session_path(self, key: str) -> Path:
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"

    @staticmethod
    def _build_session_id(
        key: str,
        created_at: datetime | None = None,
        *,
        suffix: str | None = None,
    ) -> str:
        created_at = created_at or datetime.now()
        safe_key = safe_filename(key.replace(":", "_"))
        stamp = created_at.strftime("%Y%m%dT%H%M%S")
        resolved_suffix = suffix or uuid.uuid4().hex[:8]
        return f"{safe_key}_{stamp}_{resolved_suffix}"

    @staticmethod
    def _build_dated_relative_path(session_id: str, created_at: datetime) -> str:
        return f"{created_at:%Y/%m/%d}/{session_id}.jsonl"

    def _new_session(self, key: str) -> Session:
        now = datetime.now()
        session_id = self._build_session_id(key, now)
        return Session(
            key=key,
            session_id=session_id,
            created_at=now,
            updated_at=now,
            storage_path=self._build_dated_relative_path(session_id, now),
        )

    def _resolve_session_path(self, session: Session) -> Path:
        if session.storage_path:
            return self.sessions_dir / session.storage_path

        if session.session_id:
            relative = self._build_dated_relative_path(session.session_id, session.created_at)
            session.storage_path = relative
            return self.sessions_dir / relative

        legacy = self._legacy_session_path(session.key)
        if legacy.exists():
            session.storage_path = str(legacy.relative_to(self.sessions_dir))
            return legacy

        session.session_id = self._build_session_id(session.key, session.created_at)
        relative = self._build_dated_relative_path(session.session_id, session.created_at)
        session.storage_path = relative
        return self.sessions_dir / relative

    def _load_from_path(self, path: Path, *, fallback_key: str | None = None) -> Session | None:
        if not path.exists():
            return None

        try:
            messages: list[dict[str, Any]] = []
            metadata: dict[str, Any] = {}
            key = fallback_key or ""
            session_id = ""
            created_at: datetime | None = None
            updated_at: datetime | None = None
            last_consolidated = 0

            with open(path, encoding="utf-8") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line:
                        continue

                    data = json.loads(line)
                    if data.get("_type") in {"metadata", "session_state"}:
                        key = str(data.get("key") or key or fallback_key or "")
                        session_id = str(data.get("session_id") or session_id or "")
                        metadata = data.get("metadata", metadata)
                        created_at = _parse_dt(str(data.get("created_at") or "")) or created_at
                        updated_at = _parse_dt(str(data.get("updated_at") or "")) or updated_at
                        last_consolidated = int(data.get("last_consolidated", last_consolidated) or 0)
                        continue
                    messages.append(data)

            if not key:
                if fallback_key:
                    key = fallback_key
                else:
                    raise ValueError(f"missing session key in {path}")

            created_at = created_at or _parse_dt(messages[0].get("timestamp") if messages else None) or datetime.now()
            updated_at = updated_at or _parse_dt(messages[-1].get("timestamp") if messages else None) or created_at
            session = Session(
                key=key,
                session_id=session_id or self._build_session_id(key, created_at, suffix="legacy"),
                messages=messages,
                created_at=created_at,
                updated_at=updated_at,
                metadata=metadata if isinstance(metadata, dict) else {},
                last_consolidated=last_consolidated,
                storage_path=str(path.relative_to(self.sessions_dir)),
            )
            session._saved_message_count = len(messages)
            session._saved_state_fingerprint = _state_fingerprint(
                session_id=session.session_id,
                key=session.key,
                created_at=session.created_at,
                updated_at=session.updated_at,
                metadata=session.metadata,
                last_consolidated=session.last_consolidated,
            )
            return session
        except Exception as e:
            logger.warning("Failed to load session from {}: {}", path, e)
            return None

    def _load(self, key: str) -> Session | None:
        active = self._load_active_index()
        if rel_path := active.get(key):
            path = self.sessions_dir / rel_path
            session = self._load_from_path(path, fallback_key=key)
            if session is not None:
                return session

        legacy = self._legacy_session_path(key)
        if legacy.exists():
            return self._load_from_path(legacy, fallback_key=key)
        return None

    def get_or_create(self, key: str) -> Session:
        """Get an existing active session or create a new one."""
        if key in self._cache:
            return self._cache[key]

        session = self._load(key)
        if session is None:
            session = self._new_session(key)
            self._load_active_index()[key] = session.storage_path or ""
            self._save_active_index()

        self._cache[key] = session
        return session

    def rotate(self, key: str) -> Session:
        """Start a fresh live session for ``key`` without deleting older history."""
        existing = self._cache.get(key)
        if existing is not None:
            self.save(existing)

        session = self._new_session(key)
        self._cache[key] = session
        self._load_active_index()[key] = session.storage_path or ""
        self._save_active_index()
        return session

    def save(self, session: Session) -> None:
        """Append new session records to disk and refresh the active index."""
        if not session.session_id:
            session.session_id = self._build_session_id(session.key, session.created_at)

        path = self._resolve_session_path(session)
        ensure_dir(path.parent)

        new_messages = session.messages[session._saved_message_count :]
        state_fingerprint = _state_fingerprint(
            session_id=session.session_id,
            key=session.key,
            created_at=session.created_at,
            updated_at=session.updated_at,
            metadata=session.metadata,
            last_consolidated=session.last_consolidated,
        )
        should_write_state = (
            not path.exists() or state_fingerprint != session._saved_state_fingerprint
        )

        if new_messages or should_write_state:
            with open(path, "a", encoding="utf-8") as f:
                for msg in new_messages:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
                if should_write_state:
                    state_line = {
                        "_type": "session_state",
                        "session_id": session.session_id,
                        "key": session.key,
                        "created_at": session.created_at.isoformat(),
                        "updated_at": session.updated_at.isoformat(),
                        "metadata": session.metadata,
                        "last_consolidated": session.last_consolidated,
                    }
                    f.write(json.dumps(state_line, ensure_ascii=False) + "\n")

        session.storage_path = str(path.relative_to(self.sessions_dir))
        session._saved_message_count = len(session.messages)
        session._saved_state_fingerprint = state_fingerprint

        active = self._load_active_index()
        if active.get(session.key) != session.storage_path:
            active[session.key] = session.storage_path
            self._save_active_index()

        self._cache[session.key] = session

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache."""
        self._cache.pop(key, None)

    def list_sessions(self) -> list[dict[str, Any]]:
        """List all session archives."""
        sessions: list[dict[str, Any]] = []
        for path in self.sessions_dir.rglob("*.jsonl"):
            session = self._load_from_path(path)
            if session is None:
                continue
            sessions.append(
                {
                    "key": session.key,
                    "session_id": session.session_id,
                    "created_at": session.created_at.isoformat(),
                    "updated_at": session.updated_at.isoformat(),
                    "path": str(path),
                }
            )
        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)

    def iter_sessions(self) -> list[Session]:
        """Load all archived sessions from disk."""
        items: list[Session] = []
        for path in self.sessions_dir.rglob("*.jsonl"):
            session = self._load_from_path(path)
            if session is not None:
                items.append(session)
        return sorted(items, key=lambda s: s.updated_at)
