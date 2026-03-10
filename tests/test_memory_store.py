from feibot.agent.memory import MemoryStore


def test_memory_context_omits_irrelevant_fallback_for_unmatched_query(tmp_path):
    store = MemoryStore(tmp_path)
    store.write_long_term(
        "\n".join(
            [
                "## Projects",
                "- **nanochat**: Training project.",
                "  - **Branch**: `dev-yzchen-npu`",
                "  - **Current Issue**: SwanLab logging incomplete.",
                "  - **Root Cause**: Code uses `wandb.log()` instead of `swanlab.log()`.",
            ]
        )
    )

    context = store.get_memory_context("好的，先去掉v1 然后重启，我再试试")

    assert context == ""


def test_memory_context_returns_relevant_excerpt_when_query_matches(tmp_path):
    store = MemoryStore(tmp_path)
    store.write_long_term(
        "\n".join(
            [
                "## Projects",
                "- **nanoclaw**: Node.js assistant project.",
                "",
                "## Technical Notes",
                "- `ANTHROPIC_BASE_URL` in nanoclaw should not include `/v1`.",
            ]
        )
    )

    context = store.get_memory_context("把 nanoclaw 的 /v1 去掉然后重启")

    assert "nanoclaw" in context
    assert "/v1" in context


def test_memory_context_ignores_short_substring_only_queries(tmp_path):
    store = MemoryStore(tmp_path)
    store.write_long_term(
        "\n".join(
            [
                "## Technical Notes",
                "- Feishu requires `im.message.receive_v1` subscription.",
                "- API supports `POST /open-apis/im/v1/chats`.",
            ]
        )
    )

    context = store.get_memory_context("v1")

    assert context == ""
