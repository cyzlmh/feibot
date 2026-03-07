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


def test_system_prompt_contains_only_direct_chat_spawn_policy_for_ou_chat(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path)
    prompt = ctx.build_system_prompt(
        current_message="请帮我做个任务",
        channel="feishu",
        chat_id="ou_12345",
    )

    assert "Feishu direct chat" in prompt
    assert "call `spawn` early" in prompt
    assert "web search" in prompt
    assert "research/material collation" in prompt
    assert "video summarization" in prompt
    assert "concept learning" in prompt
    assert "Feishu group chat" not in prompt


def test_system_prompt_contains_only_group_chat_spawn_policy_for_oc_chat(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path)
    prompt = ctx.build_system_prompt(
        current_message="请帮我做个任务",
        channel="feishu",
        chat_id="oc_12345",
    )

    assert "Feishu group chat" in prompt
    assert "do not auto-spawn unless the user explicitly asks" in prompt
    assert "call `spawn` early" not in prompt
