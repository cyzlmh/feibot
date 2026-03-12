"""Helpers for parsing ``channels.feishu.allow_from`` entries."""

from __future__ import annotations


def parse_allow_from_entry(raw: str | None) -> tuple[str, str]:
    """Parse ``open_id`` or legacy ``open_id:phone`` entries from allow_from."""
    value = str(raw or "").strip()
    if not value:
        return "", ""
    open_id, has_sep, phone = value.partition(":")
    open_id = open_id.strip()
    if not open_id:
        return "", ""
    return open_id, phone.strip() if has_sep else ""


def extract_allow_from_open_ids(entries: list[str] | None) -> list[str]:
    """Return normalized allow_from open IDs, dropping empty entries."""
    open_ids: list[str] = []
    for entry in entries or []:
        open_id, _ = parse_allow_from_entry(entry)
        if open_id:
            open_ids.append(open_id)
    return open_ids
