"""Shared path restriction helpers for local-file tools."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


def normalize_roots(roots: Iterable[str | Path] | None) -> list[Path]:
    """Normalize and deduplicate directory roots."""
    out: list[Path] = []
    seen: set[str] = set()
    for entry in roots or []:
        text = str(entry or "").strip()
        if not text:
            continue
        resolved = Path(text).expanduser().resolve()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        out.append(resolved)
    return out


def combine_roots(*groups: object) -> list[Path]:
    """Combine one or more directory inputs into a normalized list."""
    merged: list[str | Path] = []
    for group in groups:
        if group is None:
            continue
        if isinstance(group, (str, Path)):
            merged.append(group)
            continue
        if isinstance(group, Iterable):
            for entry in group:
                if isinstance(entry, (str, Path)):
                    merged.append(entry)
    return normalize_roots(merged)


def resolve_path(path: str | Path) -> Path:
    """Resolve a filesystem path."""
    return Path(path).expanduser().resolve()


def is_within_roots(path: str | Path, roots: Iterable[str | Path] | None = None) -> bool:
    """Return whether the resolved path stays within any configured root."""
    resolved = resolve_path(path)
    normalized = normalize_roots(roots)
    if not normalized:
        return True

    for base in normalized:
        try:
            resolved.relative_to(base)
            return True
        except ValueError:
            continue
    return False


def resolve_restricted_path(path: str | Path, roots: Iterable[str | Path] | None = None) -> Path:
    """Resolve a path and enforce that it stays within configured roots."""
    resolved = resolve_path(path)
    normalized = normalize_roots(roots)
    if not normalized:
        return resolved

    for base in normalized:
        try:
            resolved.relative_to(base)
            return resolved
        except ValueError:
            continue

    path_text = str(path)
    if len(normalized) == 1:
        raise PermissionError(f"Path {path_text} is outside writable directory {normalized[0]}")
    joined = ", ".join(str(item) for item in normalized)
    raise PermissionError(f"Path {path_text} is outside writable directory set: {joined}")


normalize_allowed_dirs = normalize_roots
combine_allowed_dirs = combine_roots
resolve_allowed_path = resolve_restricted_path
