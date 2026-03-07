import asyncio
from pathlib import Path

import pytest

from feibot.heartbeat.service import HeartbeatService, _is_heartbeat_empty
from feibot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class FakeProvider(LLMProvider):
    def __init__(self, response: LLMResponse):
        self._response = response

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        return self._response

    def get_default_model(self) -> str:
        return "fake-model"


def _write_heartbeat(path: Path, content: str) -> None:
    (path / "HEARTBEAT.md").write_text(content, encoding="utf-8")


def test_is_heartbeat_empty_skips_non_actionable_content() -> None:
    assert _is_heartbeat_empty(None)
    assert _is_heartbeat_empty("")
    assert _is_heartbeat_empty("# Title\n\n<!-- comment -->\n- [ ]\n* [x]")
    assert not _is_heartbeat_empty("# Tasks\n- [ ] Check alerts")


@pytest.mark.asyncio
async def test_start_is_idempotent(tmp_path: Path) -> None:
    provider = FakeProvider(LLMResponse(content=""))
    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="fake-model",
        interval_s=9999,
        enabled=True,
    )

    await service.start()
    first_task = service._task
    await service.start()

    assert service._task is first_task

    service.stop()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_tick_executes_and_notifies_when_decision_is_run(tmp_path: Path) -> None:
    _write_heartbeat(tmp_path, "- [ ] Check alerts")
    provider = FakeProvider(
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="call_1",
                    name="heartbeat",
                    arguments={"action": "run", "tasks": "Check alerts and summarize"},
                )
            ],
        )
    )
    seen: dict[str, str] = {}

    async def _on_execute(tasks: str) -> str:
        seen["tasks"] = tasks
        return "done"

    async def _on_notify(response: str) -> None:
        seen["response"] = response

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="fake-model",
        on_execute=_on_execute,
        on_notify=_on_notify,
    )

    await service._tick()

    assert seen["tasks"] == "Check alerts and summarize"
    assert seen["response"] == "done"


@pytest.mark.asyncio
async def test_tick_skips_execute_when_decision_is_skip(tmp_path: Path) -> None:
    _write_heartbeat(tmp_path, "- [ ] Check alerts")
    provider = FakeProvider(
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="call_1",
                    name="heartbeat",
                    arguments={"action": "skip"},
                )
            ],
        )
    )
    executed = False

    async def _on_execute(tasks: str) -> str:
        nonlocal executed
        executed = True
        return tasks

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="fake-model",
        on_execute=_on_execute,
    )

    await service._tick()
    assert executed is False
