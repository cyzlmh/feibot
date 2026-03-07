from pathlib import Path

import pytest

from feibot.agent.tools.shell import ExecTool


@pytest.mark.asyncio
async def test_exec_tool_path_append_allows_custom_binary(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool_script = bin_dir / "feibot-test-tool"
    tool_script.write_text("#!/bin/sh\necho path_append_ok\n", encoding="utf-8")
    tool_script.chmod(0o755)

    tool = ExecTool(
        timeout=5,
        working_dir=str(tmp_path),
        path_append=str(bin_dir),
    )

    result = await tool.execute("feibot-test-tool")
    assert "path_append_ok" in result
