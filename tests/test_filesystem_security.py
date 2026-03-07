from pathlib import Path

import pytest

from feibot.agent.tools.filesystem import ReadFileTool


@pytest.mark.asyncio
async def test_read_file_blocks_prefix_path_traversal(tmp_path: Path) -> None:
    allowed_dir = tmp_path / "workspace"
    outside_prefixed = tmp_path / "workspace_evil"
    allowed_dir.mkdir()
    outside_prefixed.mkdir()
    target = outside_prefixed / "secret.txt"
    target.write_text("top secret", encoding="utf-8")

    tool = ReadFileTool(allowed_dir=allowed_dir)
    result = await tool.execute(str(target))

    assert "outside allowed directory" in result


@pytest.mark.asyncio
async def test_read_file_rejects_very_large_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "huge.txt"
    target.write_text("a" * 600_000, encoding="utf-8")

    tool = ReadFileTool(allowed_dir=workspace)
    result = await tool.execute(str(target))

    assert result.startswith("Error: File too large")
    assert "head/tail/grep" in result


@pytest.mark.asyncio
async def test_read_file_truncates_long_content(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "long.txt"
    target.write_text("b" * 130_000, encoding="utf-8")

    tool = ReadFileTool(allowed_dir=workspace)
    result = await tool.execute(str(target))

    assert result.startswith("b" * 100)
    assert "(truncated - file is 130,000 chars, limit 128,000)" in result
