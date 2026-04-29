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
    assert "`/fork`" not in prompt


def test_system_prompt_has_no_spawn_policy_for_oc_chat(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path)
    prompt = ctx.build_system_prompt(
        current_message="请帮我做个任务",
        channel="feishu",
        chat_id="oc_12345",
    )

    assert "do not auto-spawn" not in prompt
    assert "`/fork`" not in prompt


def test_system_prompt_preloads_long_term_memory(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path)

    # Create minimal AGENTS.md
    (tmp_path / "AGENTS.md").write_text("# Agent Instructions\n\nYou are a helpful assistant.", encoding="utf-8")

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
    assert "# Long-term Memory" in prompt


def test_system_prompt_loads_agents_md_from_workspace(tmp_path: Path) -> None:
    """System prompt should come entirely from workspace AGENTS.md."""
    ctx = ContextBuilder(tmp_path)

    # Create custom AGENTS.md
    (tmp_path / "AGENTS.md").write_text(
        "# Custom Agent\n\nI am a custom agent with specific instructions.\n",
        encoding="utf-8",
    )

    prompt = ctx.build_system_prompt(
        current_message="hello",
        channel="feishu",
        chat_id="ou_12345",
    )

    assert "I am a custom agent with specific instructions." in prompt
    assert "# Custom Agent" in prompt


def test_context_builder_honors_disable_builtin_skills_env(tmp_path: Path) -> None:
    local_skill = tmp_path / "skills" / "local-skill"
    local_skill.mkdir(parents=True)
    (local_skill / "SKILL.md").write_text(
        "---\nname: local-skill\ndescription: local\n---\nUse local skill.\n",
        encoding="utf-8",
    )
    builtin_dir = tmp_path / "builtin-skills"
    builtin_skill = builtin_dir / "builtin-skill"
    builtin_skill.mkdir(parents=True)
    (builtin_skill / "SKILL.md").write_text(
        "---\nname: builtin-skill\ndescription: builtin\n---\nUse builtin skill.\n",
        encoding="utf-8",
    )

    ctx = ContextBuilder(
        tmp_path,
        skills_env={"FEIBOT_DISABLE_BUILTIN_SKILLS": "1"},
        builtin_skills_dir=builtin_dir,
    )

    prompt = ctx.build_system_prompt(current_message="hello")

    assert "local-skill" in prompt
    assert "builtin-skill" not in prompt
