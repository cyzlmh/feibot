"""Offline migration helpers for the new memory/history layout."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from feibot.agent.memory import MemoryStore
from feibot.history.service import HistorySyncService, SessionHistorySummary
from feibot.session.manager import Session, SessionManager


@dataclass
class MigrationSummary:
    migrated_sessions: int = 0
    migrated_logs: int = 0
    archived_session_files: int = 0
    archived_log_files: int = 0
    history_entries: int = 0
    session_backup_dir: str = ""
    log_backup_dir: str = ""


def _derive_key_from_stem(stem: str) -> str:
    if "_" not in stem:
        return stem
    channel, chat_id = stem.split("_", 1)
    return f"{channel}:{chat_id}"


def _copy_jsonl(src: Path, dst: Path, *, session_id: str | None = None) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, encoding="utf-8") as rf, open(dst, "w", encoding="utf-8") as wf:
        for raw_line in rf:
            line = raw_line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if session_id and isinstance(data, dict) and data.get("_type") is None:
                data.setdefault("session_id", session_id)
            wf.write(json.dumps(data, ensure_ascii=False) + "\n")


def _build_session_block(
    history_sync: HistorySyncService,
    session: Session,
) -> SessionHistorySummary:
    return SessionHistorySummary(
        summary=history_sync._fallback_summary(session),
        keywords=history_sync._fallback_keywords(session),
        memory_candidates=[],
    )


def migrate_workspace(root: Path) -> MigrationSummary:
    """
    Migrate a live feibot workspace to the new session/history layout.

    This migration is intentionally offline:
    - rewrites flat session/log files into dated ``session_id``-based archives
    - preserves the old consolidated HISTORY as a legacy section
    - generates deterministic fallback session summaries
    - does not modify MEMORY.md
    - does not generate memory candidates automatically
    """

    root = root.expanduser().resolve()
    workspace = root / "workspace"
    sessions_dir = root / "sessions"
    logs_dir = workspace / "logs"
    memory = MemoryStore(workspace)
    manager = SessionManager(sessions_dir)
    history_sync = HistorySyncService(
        workspace=workspace,
        session_manager=manager,
        provider=None,  # offline migration path; summarizer is not used
        model="offline-migration",
    )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_backup_dir = root / f"sessions_legacy_flat_{stamp}"
    log_backup_dir = workspace / f"logs_legacy_flat_{stamp}"

    summary = MigrationSummary(
        session_backup_dir=str(session_backup_dir),
        log_backup_dir=str(log_backup_dir),
    )

    flat_session_files = sorted(p for p in sessions_dir.glob("*.jsonl") if p.is_file())
    session_by_stem: dict[str, Session] = {}
    active_index: dict[str, str] = {}

    for path in flat_session_files:
        key = _derive_key_from_stem(path.stem)
        session = manager._load_from_path(path, fallback_key=key)
        if session is None:
            continue
        session.storage_path = manager._build_dated_relative_path(session.session_id, session.created_at)
        session._saved_message_count = 0
        session._saved_state_fingerprint = ""
        manager.save(session)
        active_index[session.key] = session.storage_path or ""
        session_by_stem[path.stem] = session
        summary.migrated_sessions += 1

    if active_index:
        manager._active_index = active_index
        manager._save_active_index()

    flat_log_files = sorted(p for p in logs_dir.glob("*.jsonl") if p.is_file())
    for path in flat_log_files:
        session = session_by_stem.get(path.stem)
        if session is not None:
            dst = logs_dir / f"{session.created_at:%Y/%m/%d}" / f"{session.session_id}.jsonl"
            _copy_jsonl(path, dst, session_id=session.session_id)
            summary.migrated_logs += 1
            continue

        # Preserve unmatched logs in a dated archive using their file timestamp.
        created_at = datetime.fromtimestamp(path.stat().st_mtime)
        key = _derive_key_from_stem(path.stem)
        synthetic_id = manager._build_session_id(key, created_at, suffix="logonly")
        dst = logs_dir / f"{created_at:%Y/%m/%d}" / f"{synthetic_id}.jsonl"
        _copy_jsonl(path, dst, session_id=synthetic_id)
        summary.migrated_logs += 1

    if flat_session_files:
        session_backup_dir.mkdir(parents=True, exist_ok=True)
        for path in flat_session_files:
            shutil.move(str(path), str(session_backup_dir / path.name))
            summary.archived_session_files += 1

    if flat_log_files:
        log_backup_dir.mkdir(parents=True, exist_ok=True)
        for path in flat_log_files:
            shutil.move(str(path), str(log_backup_dir / path.name))
            summary.archived_log_files += 1

    legacy_history = memory.read_history().strip()
    legacy_backup_path = memory.memory_dir / "HISTORY.legacy.md"
    if legacy_history:
        legacy_backup_path.write_text(legacy_history.rstrip() + "\n", encoding="utf-8")

    content_parts = ["# Legacy History", ""]
    if legacy_history:
        content_parts.extend(
            [
                "This section preserves the pre-migration consolidation log.",
                "",
                legacy_history.rstrip(),
                "",
            ]
        )
    else:
        content_parts.extend(
            [
                "No pre-migration consolidated history was found.",
                "",
            ]
        )
    content_parts.extend(["# Session History", ""])
    history_content = "\n".join(content_parts)

    state: dict[str, dict[str, Any]] = {}
    sessions = manager.iter_sessions()
    for session in sessions:
        if not session.messages:
            continue
        session_summary = _build_session_block(history_sync, session)
        history_content = history_sync._upsert_history_block(
            history_content,
            session=session,
            summary=session_summary,
        )
        state[session.session_id] = {
            "updated_at": session.updated_at.isoformat(),
            "message_count": len(session.messages),
        }
        summary.history_entries += 1

    memory.write_history(history_content.rstrip() + "\n")
    memory.write_review(
        "# Memory Review\n\n"
        "Offline migration completed.\n\n"
        "No automatic memory candidates were generated during migration.\n"
        "Review future nightly recommendations before updating MEMORY.md.\n"
    )
    history_sync._save_state(state)
    return summary
