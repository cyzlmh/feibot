"""Utility functions for feibot."""

from pathlib import Path
from datetime import datetime



_DATA_ROOT: Path = Path.home() / ".feibot"


def set_data_root(path: Path) -> None:
    """Set the feibot data root directory."""
    global _DATA_ROOT
    _DATA_ROOT = path.expanduser().resolve()


def ensure_dir(path: Path) -> Path:
    """Ensure a directory exists, creating it if necessary."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_data_path() -> Path:
    """Get the feibot data directory (default: ~/.feibot)."""
    return ensure_dir(_DATA_ROOT)


def get_workspace_path(workspace: str | None = None) -> Path:
    """
    Get the workspace path.
    
    Args:
        workspace: Optional workspace path. Defaults to <data_root>/workspace.
    
    Returns:
        Expanded and ensured workspace path.
    """
    if workspace:
        path = Path(workspace).expanduser()
    else:
        path = get_data_path() / "workspace"
    return ensure_dir(path)


def get_sessions_path() -> Path:
    """Get the sessions storage directory."""
    return ensure_dir(get_data_path() / "sessions")


def get_history_path() -> Path:
    """Get the history storage directory."""
    return ensure_dir(get_data_path() / "history")


def get_skills_path(workspace: Path | None = None) -> Path:
    """Get the skills directory within the workspace."""
    ws = workspace or get_workspace_path()
    return ensure_dir(ws / "skills")


def timestamp() -> str:
    """Get current timestamp in ISO format."""
    return datetime.now().isoformat()


def truncate_string(s: str, max_len: int = 100, suffix: str = "...") -> str:
    """Truncate a string to max length, adding suffix if truncated."""
    if len(s) <= max_len:
        return s
    return s[: max_len - len(suffix)] + suffix


def safe_filename(name: str) -> str:
    """Convert a string to a safe filename."""
    # Replace unsafe characters
    unsafe = '<>:"/\\|?*'
    for char in unsafe:
        name = name.replace(char, "_")
    return name.strip()


def parse_session_key(key: str) -> tuple[str, str]:
    """
    Parse a session key into channel and chat_id.
    
    Args:
        key: Session key in format "channel:chat_id"
    
    Returns:
        Tuple of (channel, chat_id)
    """
    parts = key.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid session key: {key}")
    return parts[0], parts[1]


def sync_workspace_templates(workspace: Path, silent: bool = False) -> list[str]:
    """Sync bundled templates into workspace, creating only missing files."""
    from importlib.resources import files as pkg_files

    try:
        templates_dir = pkg_files("feibot") / "templates"
    except Exception:
        return []
    if not templates_dir.is_dir():
        return []

    added: list[str] = []

    def _write_text(dest: Path, content: str) -> None:
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        added.append(str(dest.relative_to(workspace)))

    for item in templates_dir.iterdir():
        if item.name.endswith(".md"):
            _write_text(workspace / item.name, item.read_text(encoding="utf-8"))

    _write_text(
        workspace / "memory" / "MEMORY.md",
        (templates_dir / "memory" / "MEMORY.md").read_text(encoding="utf-8"),
    )
    _write_text(workspace / "memory" / "HISTORY.md", "")
    ensure_dir(workspace / "skills")

    if added and not silent:
        from rich.console import Console

        console = Console()
        for path in added:
            console.print(f"  [dim]Created {path}[/dim]")
    return added
