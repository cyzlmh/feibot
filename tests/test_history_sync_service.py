import pytest

from feibot.history.service import HistorySyncService
from feibot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from feibot.session.manager import SessionManager


class _HistoryProvider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        return LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(
                    id="call_1",
                    name="sync_session_history",
                    arguments={
                        "summary": "User discussed feibot memory redesign and agreed on nightly history sync.",
                        "keywords": ["feibot", "memory", "history-sync"],
                        "memory_candidates": [
                            {
                                "candidate": "Prefer nightly HISTORY.md sync over inline consolidation.",
                                "reason": "This is a durable operating policy for feibot memory handling.",
                            }
                        ],
                    },
                )
            ],
        )

    def get_default_model(self) -> str:
        return "dummy/test-model"


@pytest.mark.asyncio
async def test_history_sync_updates_history_and_review_without_touching_memory(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True, exist_ok=True)
    memory_path = workspace / "memory" / "MEMORY.md"
    memory_path.write_text(
        "# Long-term Memory\n\n## Stable Tooling Preferences\n- Prefer `uv` over `python`.\n",
        encoding="utf-8",
    )

    manager = SessionManager(tmp_path / "sessions")
    session = manager.get_or_create("feishu:chat1")
    session.add_message("user", "Let's redesign feibot memory.")
    session.add_message("assistant", "We should use nightly history sync.")
    manager.save(session)

    service = HistorySyncService(
        workspace=workspace,
        session_manager=manager,
        provider=_HistoryProvider(),
        model="dummy/test-model",
    )

    result = await service.run()

    history_text = (workspace / "memory" / "HISTORY.md").read_text(encoding="utf-8")
    review_text = (workspace / "memory" / "REVIEW.md").read_text(encoding="utf-8")

    assert result is not None
    assert "Daily reflection report" in result
    assert session.session_id in history_text
    assert "nightly history sync" in history_text.lower()
    assert "Prefer nightly HISTORY.md sync over inline consolidation." in review_text
    assert "possible memory item" in review_text.lower()
    assert memory_path.read_text(encoding="utf-8").startswith("# Long-term Memory")

    second = await service.run()
    assert second == "Daily reflection report: no updated sessions since the last run."
