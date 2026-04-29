import asyncio
from pathlib import Path

from feibot.agent.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool


def test_read_file_allows_outside_writable_dirs(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.txt"
    target.write_text("top secret", encoding="utf-8")

    tool = ReadFileTool()
    result = asyncio.run(tool.execute(str(target)))

    assert result == "top secret"


def test_write_file_blocks_outside_writable_dirs(tmp_path: Path) -> None:
    writable = tmp_path / "workspace"
    writable.mkdir()
    blocked = tmp_path / "outside" / "blocked.txt"

    tool = WriteFileTool(writable_dirs=[writable])
    result = asyncio.run(tool.execute(str(blocked), "nope"))

    assert "outside writable directory" in result


def test_edit_file_allows_inside_writable_dirs(tmp_path: Path) -> None:
    writable = tmp_path / "workspace"
    writable.mkdir()
    target = writable / "note.txt"
    target.write_text("hello world", encoding="utf-8")

    tool = EditFileTool(writable_dirs=[writable])
    result = asyncio.run(tool.execute(str(target), "world", "feibot"))

    assert result == f"Successfully edited {target}"
    assert target.read_text(encoding="utf-8") == "hello feibot"


def test_read_file_rejects_very_large_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "huge.txt"
    target.write_text("a" * 600_000, encoding="utf-8")

    tool = ReadFileTool()
    result = asyncio.run(tool.execute(str(target)))

    assert result.startswith("Error: File too large")
    assert "head/tail/grep" in result


def test_read_file_truncates_long_content(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "long.txt"
    target.write_text("b" * 130_000, encoding="utf-8")

    tool = ReadFileTool()
    result = asyncio.run(tool.execute(str(target)))

    assert result.startswith("b" * 100)
    assert "(truncated - file is 130,000 chars, limit 128,000)" in result
