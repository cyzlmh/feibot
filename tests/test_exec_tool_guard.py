from pathlib import Path

import pytest

from feibot.agent.tools.shell import ExecTool


@pytest.mark.asyncio
async def test_exec_allows_reading_outside_writable_dirs(tmp_path: Path) -> None:
    writable = tmp_path / "workspace"
    outside = tmp_path / "outside"
    writable.mkdir()
    outside.mkdir()
    target = outside / "note.txt"
    target.write_text("hello", encoding="utf-8")

    tool = ExecTool(timeout=5, working_dir=str(tmp_path), writable_dirs=[str(writable)])
    result = await tool.execute(f"cat {target}")

    assert result.strip() == "hello"


@pytest.mark.asyncio
async def test_exec_blocks_redirect_write_outside_writable_dirs(tmp_path: Path) -> None:
    writable = tmp_path / "workspace"
    writable.mkdir()

    tool = ExecTool(timeout=5, working_dir=str(tmp_path), writable_dirs=[str(writable)])
    result = await tool.execute(f"echo hi > {tmp_path / 'outside.txt'}")

    assert "outside writableDirs" in result


@pytest.mark.asyncio
async def test_exec_allows_redirect_write_inside_writable_dirs(tmp_path: Path) -> None:
    writable = tmp_path / "workspace"
    writable.mkdir()
    target = writable / "note.txt"

    tool = ExecTool(timeout=5, working_dir=str(tmp_path), writable_dirs=[str(writable)])
    result = await tool.execute(f"echo hi > {target}")

    assert "outside writableDirs" not in result
    assert target.read_text(encoding="utf-8").strip() == "hi"


@pytest.mark.asyncio
async def test_exec_blocks_rm_outside_writable_dirs(tmp_path: Path) -> None:
    writable = tmp_path / "workspace"
    outside = tmp_path / "outside"
    writable.mkdir()
    outside.mkdir()
    target = outside / "delete-me.txt"
    target.write_text("x", encoding="utf-8")

    tool = ExecTool(timeout=5, working_dir=str(tmp_path), writable_dirs=[str(writable)])
    result = await tool.execute(f"rm {target}")

    assert "outside writableDirs" in result
    assert target.exists()


@pytest.mark.asyncio
async def test_exec_allows_copy_into_writable_dirs(tmp_path: Path) -> None:
    writable = tmp_path / "workspace"
    source_dir = tmp_path / "source"
    writable.mkdir()
    source_dir.mkdir()
    source = source_dir / "src.txt"
    source.write_text("copy me", encoding="utf-8")
    target = writable / "dst.txt"

    tool = ExecTool(timeout=5, working_dir=str(tmp_path), writable_dirs=[str(writable)])
    result = await tool.execute(f"cp {source} {target}")

    assert "outside writableDirs" not in result
    assert target.read_text(encoding="utf-8") == "copy me"


def test_exec_blocks_ssh_host_outside_allowed_hosts(tmp_path: Path) -> None:
    tool = ExecTool(
        timeout=5,
        working_dir=str(tmp_path),
        writable_dirs=[str(tmp_path)],
        allowed_hosts=["buildbox.internal"],
    )

    guard = tool._guard_command("ssh admin@example.com uptime", str(tmp_path))
    assert "not in allowedHosts" in str(guard)


def test_exec_allows_scp_host_in_allowed_hosts(tmp_path: Path) -> None:
    tool = ExecTool(
        timeout=5,
        working_dir=str(tmp_path),
        writable_dirs=[str(tmp_path)],
        allowed_hosts=["buildbox.internal"],
    )

    guard = tool._guard_command(
        "scp release.tar admin@buildbox.internal:/srv/releases/",
        str(tmp_path),
    )
    assert guard is None


def test_exec_allows_command_when_allowed_hosts_is_empty(tmp_path: Path) -> None:
    tool = ExecTool(timeout=5, working_dir=str(tmp_path), writable_dirs=[str(tmp_path)], allowed_hosts=[])

    guard = tool._guard_command("ssh admin@example.com uptime", str(tmp_path))
    assert guard is None


def test_exec_parse_approval_pending_id_compatibility() -> None:
    assert ExecTool.parse_approval_pending_id("approval-pending:abc123") == "abc123"
    assert ExecTool.parse_approval_pending_id("not-an-approval") is None
    assert ExecTool.parse_approval_pending_id(None) is None


@pytest.mark.asyncio
async def test_exec_run_command_returns_raw_output_and_exit_code(tmp_path: Path) -> None:
    tool = ExecTool(timeout=5, working_dir=str(tmp_path))

    output, returncode = await tool.run_command("printf ''")

    assert output == ""
    assert returncode == 0
