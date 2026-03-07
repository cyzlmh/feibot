"""Channel log store for raw conversation records and session backfill."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from feibot.session.manager import Session
from feibot.utils.helpers import ensure_dir, safe_filename


@dataclass
class LogEntry:
    """Raw channel log entry."""

    role: str
    content: str
    timestamp: str
    message_id: str | None = None
    sender_id: str | None = None
    channel: str | None = None
    chat_id: str | None = None
    metadata: dict[str, Any] | None = None


class ChannelLogStore:
    """
    Persistent raw message log.

    - Stores channel records in `workspace/logs/<session>.jsonl`.
    - Can sync user records back into Session when context store misses messages.
    """

    def __init__(self, logs_dir: Path):
        self.logs_dir = ensure_dir(logs_dir)
        self._seen_user_ids: dict[str, set[str]] = {}

    def _get_log_path(self, session_key: str) -> Path:
        safe_key = safe_filename(session_key.replace(":", "_"))
        return self.logs_dir / f"{safe_key}.jsonl"

    def _load_seen_user_ids(self, session_key: str) -> set[str]:
        if session_key in self._seen_user_ids:
            return self._seen_user_ids[session_key]

        seen: set[str] = set()
        path = self._get_log_path(session_key)
        if path.exists():
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get("role") == "user" and data.get("message_id"):
                        seen.add(str(data["message_id"]))

        self._seen_user_ids[session_key] = seen
        return seen

    def append(self, session_key: str, entry: LogEntry) -> None:
        """Append a raw log entry. User entries are deduped by message_id."""
        path = self._get_log_path(session_key)
        payload: dict[str, Any] = {
            "role": entry.role,
            "content": entry.content,
            "timestamp": entry.timestamp,
        }
        if entry.message_id:
            payload["message_id"] = entry.message_id
        if entry.sender_id:
            payload["sender_id"] = entry.sender_id
        if entry.channel:
            payload["channel"] = entry.channel
        if entry.chat_id:
            payload["chat_id"] = entry.chat_id
        if entry.metadata:
            payload["metadata"] = entry.metadata

        if entry.role == "user" and entry.message_id:
            seen_ids = self._load_seen_user_ids(session_key)
            if entry.message_id in seen_ids:
                return
            seen_ids.add(entry.message_id)

        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def sync_users_to_session(
        self,
        session_key: str,
        session: Session,
        *,
        exclude_message_id: str | None = None,
        after_timestamp: str | None = None,
    ) -> int:
        """
        Backfill user records from raw log into session context store.

        Returns the number of user records added to the session.
        """
        path = self._get_log_path(session_key)
        if not path.exists():
            return 0

        existing_ids = {
            str(m.get("message_id"))
            for m in session.messages
            if m.get("role") == "user" and m.get("message_id")
        }
        # Fallback dedupe when historical records don't have message_id.
        existing_content = {
            str(m.get("content", ""))
            for m in session.messages
            if m.get("role") == "user" and m.get("content")
        }

        additions: list[dict[str, Any]] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if data.get("role") != "user":
                    continue

                message_id = data.get("message_id")
                if exclude_message_id and message_id == exclude_message_id:
                    continue
                if message_id and message_id in existing_ids:
                    continue
                timestamp = str(data.get("timestamp") or "")
                if after_timestamp and timestamp and timestamp <= after_timestamp:
                    continue

                content = str(data.get("content") or "")
                if not content:
                    continue
                if not message_id and content in existing_content:
                    continue

                additions.append(
                    {
                        "role": "user",
                        "content": content,
                        "timestamp": timestamp or datetime.now().isoformat(),
                        "message_id": message_id,
                        "sender_id": data.get("sender_id"),
                        "channel": data.get("channel"),
                        "chat_id": data.get("chat_id"),
                    }
                )
                if message_id:
                    existing_ids.add(message_id)
                existing_content.add(content)

        if not additions:
            return 0

        additions.sort(key=lambda x: str(x.get("timestamp", "")))
        session.messages.extend(additions)
        session.updated_at = datetime.now()
        return len(additions)
