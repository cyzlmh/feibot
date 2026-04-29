import pytest

from feibot.agent.tools.cron import CronTool


class _DummyCronService:
    def __init__(self) -> None:
        self.called = False

    def upsert_job(self, **kwargs):  # noqa: ANN003
        self.called = True
        raise AssertionError("upsert_job should not be called for invalid datetime input")

    def list_jobs(self):
        return []

    def remove_job(self, job_id: str) -> bool:
        return False


@pytest.mark.asyncio
async def test_cron_tool_rejects_invalid_iso_datetime() -> None:
    cron = _DummyCronService()
    tool = CronTool(cron)
    tool.set_context(channel="cli", chat_id="direct")

    result = await tool.execute(action="add", message="ping", at="2026-99-99T25:61:00")

    assert "Error: invalid ISO datetime format" in result
    assert "Expected format: YYYY-MM-DDTHH:MM:SS" in result
    assert cron.called is False
