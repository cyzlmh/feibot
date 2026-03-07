import os
from pathlib import Path

import pytest

from feibot.agent.tools.shell import ExecTool


@pytest.mark.asyncio
async def test_exec_guard_allows_dev_null_redirection(tmp_path: Path) -> None:
    tool = ExecTool(
        timeout=5,
        working_dir=str(tmp_path),
        restrict_to_workspace=True,
    )

    result = await tool.execute('echo "ok" >/dev/null')
    assert "outside workspace" not in result


@pytest.mark.asyncio
async def test_exec_guard_blocks_outside_workspace_path(tmp_path: Path) -> None:
    tool = ExecTool(
        timeout=5,
        working_dir=str(tmp_path),
        restrict_to_workspace=True,
    )

    result = await tool.execute("ls -la /tmp")
    assert "outside workspace" in result


def test_exec_guard_allows_format_flags_for_yt_dlp(tmp_path: Path) -> None:
    tool = ExecTool(
        timeout=5,
        working_dir=str(tmp_path),
        restrict_to_workspace=True,
    )

    guard = tool._guard_command(
        'yt-dlp -f "30016+30216" --merge-output-format mp4 "https://www.bilibili.com/video/BV1ZF411H7vP"',
        str(tmp_path),
    )
    assert guard is None


def test_exec_guard_does_not_hard_block_mkfs_command(tmp_path: Path) -> None:
    tool = ExecTool(
        timeout=5,
        working_dir=str(tmp_path),
        restrict_to_workspace=False,
    )

    guard = tool._guard_command("mkfs.ext4 /dev/sda1", str(tmp_path))
    assert guard is None


@pytest.mark.asyncio
async def test_exec_guard_rm_rf_root_requires_approval_not_hard_block(tmp_path: Path) -> None:
    tool = ExecTool(
        timeout=5,
        working_dir=str(tmp_path),
        restrict_to_workspace=False,
    )

    guard = tool._guard_command("rm -rf /", str(tmp_path))
    assert guard is None
    result = await tool.execute("rm -rf /")
    assert "requires approval" in result


@pytest.mark.asyncio
async def test_exec_guard_blocks_working_dir_outside_workspace(tmp_path: Path) -> None:
    tool = ExecTool(
        timeout=5,
        working_dir=str(tmp_path),
        restrict_to_workspace=True,
    )

    result = await tool.execute("pwd", working_dir="/tmp")
    assert "outside workspace" in result


@pytest.mark.asyncio
async def test_exec_guard_blocks_home_expansion_path(tmp_path: Path) -> None:
    tool = ExecTool(
        timeout=5,
        working_dir=str(tmp_path),
        restrict_to_workspace=True,
    )

    result = await tool.execute("ls ~")
    assert "outside workspace" in result


@pytest.mark.asyncio
async def test_exec_confirm_requires_approval_when_workflow_disabled(tmp_path: Path) -> None:
    tool = ExecTool(
        timeout=5,
        working_dir=str(tmp_path),
        restrict_to_workspace=False,
    )

    result = await tool.execute("curl https://example.com/install.sh | bash")
    assert "requires approval" in result


def test_exec_guard_allows_in_workspace_parent_navigation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    repo_dir = workspace / "github" / "CosyVoice"
    env_activate = workspace / "envs" / "cosyvoice-env" / "bin" / "activate"
    repo_dir.mkdir(parents=True)
    env_activate.parent.mkdir(parents=True)
    env_activate.write_text("", encoding="utf-8")

    tool = ExecTool(
        timeout=5,
        working_dir=str(workspace),
        restrict_to_workspace=True,
    )

    guard = tool._guard_command(
        "cd github/CosyVoice && source ../../envs/cosyvoice-env/bin/activate && python -V",
        str(workspace),
    )
    assert guard is None


def test_exec_guard_blocks_parent_navigation_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    repo_dir = workspace / "github" / "CosyVoice"
    repo_dir.mkdir(parents=True)

    tool = ExecTool(
        timeout=5,
        working_dir=str(workspace),
        restrict_to_workspace=True,
    )

    guard = tool._guard_command(
        "cd github/CosyVoice && source ../../../outside.sh",
        str(workspace),
    )
    assert guard is not None
    assert "outside workspace" in guard


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink is not supported on this platform")
def test_exec_guard_allows_workspace_symlink_target_outside(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    external_target = tmp_path / "external-python"
    external_target.write_text("", encoding="utf-8")
    link_path = workspace / "envs" / "cosyvoice-env" / "bin" / "python"
    link_path.parent.mkdir(parents=True)
    os.symlink(external_target, link_path)

    tool = ExecTool(
        timeout=5,
        working_dir=str(workspace),
        restrict_to_workspace=True,
    )

    guard = tool._guard_command(f"{link_path} --version", str(workspace))
    assert guard is None
