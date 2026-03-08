"""Helpers for parsing ``channels.feishu.allow_from`` entries."""

from __future__ import annotations


def parse_allow_from_entry(raw: str | None) -> tuple[str, str]:
    """Parse ``open_id`` or ``open_id:msisdn`` entries from allow_from."""
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


def extract_allow_from_msisdn_map(entries: list[str] | None) -> dict[str, str]:
    """Build a requester open_id -> phone map from allow_from entries."""
    mapping: dict[str, str] = {}
    for entry in entries or []:
        open_id, phone = parse_allow_from_entry(entry)
        if open_id and phone:
            mapping[open_id] = phone
    return mapping
