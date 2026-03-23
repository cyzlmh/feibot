from pathlib import Path

from feibot.agent.context import ContextBuilder


def test_build_messages_injects_runtime_context_as_untrusted_user_message(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path)
    messages = ctx.build_messages(
        history=[],
        current_message="hello",
        channel="feishu",
        chat_id="oc_group_1",
    )

    assert messages[0]["role"] == "system"
    assert "## Current Session" not in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert messages[1]["content"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
    assert "Channel: feishu" in messages[1]["content"]
    assert "Chat ID: oc_group_1" in messages[1]["content"]
    assert messages[2] == {"role": "user", "content": "hello"}


def test_system_prompt_has_no_spawn_policy_for_ou_chat(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path)
    prompt = ctx.build_system_prompt(
        current_message="请帮我做个任务",
        channel="feishu",
        chat_id="ou_12345",
    )

    assert "do not auto-spawn" not in prompt
    assert "`/sp`" not in prompt


def test_system_prompt_has_no_spawn_policy_for_oc_chat(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path)
    prompt = ctx.build_system_prompt(
        current_message="请帮我做个任务",
        channel="feishu",
        chat_id="oc_12345",
    )

    assert "do not auto-spawn" not in prompt
    assert "`/sp`" not in prompt


def test_system_prompt_preloads_long_term_memory(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(exist_ok=True)
    (memory_dir / "MEMORY.md").write_text(
        "## Technical Notes\n- SwanLab logging issue exists in nanochat.\n",
        encoding="utf-8",
    )

    prompt = ctx.build_system_prompt(
        current_message="好的，先去掉v1 然后重启，我再试试",
        channel="feishu",
        chat_id="oc_group_1",
    )

    assert "SwanLab logging issue exists in nanochat." in prompt
    assert "`memory/MEMORY.md` is preloaded into every prompt." in prompt
    assert "Do not add anything to `memory/MEMORY.md` unless the user explicitly approves it." in prompt
