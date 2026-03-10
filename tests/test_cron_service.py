import asyncio

import pytest

from feibot.cron.service import CronService
from feibot.cron.types import CronSchedule


def test_add_job_rejects_unknown_timezone(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    with pytest.raises(ValueError, match="unknown timezone 'America/Vancovuer'"):
        service.upsert_job(
            name="tz typo",
            schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="America/Vancovuer"),
            message="hello",
        )

    assert service.list_jobs(include_disabled=True) == []


def test_add_job_accepts_valid_timezone(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    job, status = service.upsert_job(
        name="tz ok",
        schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="America/Vancouver"),
        message="hello",
    )

    assert status == "created"
    assert job.schedule.tz == "America/Vancouver"
    assert job.state.next_run_at_ms is not None


def test_upsert_job_same_identity_is_idempotent(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    schedule = CronSchedule(kind="every", every_ms=60_000)

    first, first_status = service.upsert_job(
        name="blog-check",
        schedule=schedule,
        message="Check blog updates",
        deliver=True,
        channel="feishu",
        to="oc_123",
    )
    second, second_status = service.upsert_job(
        name="blog-check",
        schedule=schedule,
        message="Check    blog   updates",
        deliver=True,
        channel="feishu",
        to="oc_123",
    )

    jobs = service.list_jobs(include_disabled=True)
    assert first_status == "created"
    assert second_status == "unchanged"
    assert len(jobs) == 1
    assert first.id == second.id


def test_upsert_job_updates_delivery_flags_for_same_identity(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    first, first_status = service.upsert_job(
        name="blog-check",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="Check blog updates",
        deliver=False,
        channel="feishu",
        to="oc_123",
    )
    second, second_status = service.upsert_job(
        name="blog-check",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="Check blog updates",
        deliver=True,
        channel="feishu",
        to="oc_123",
    )

    jobs = service.list_jobs(include_disabled=True)
    assert first_status == "created"
    assert second_status == "updated"
    assert len(jobs) == 1
    assert first.id == second.id
    assert second.payload.deliver is True


def test_upsert_job_updates_schedule_for_same_identity(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    first, first_status = service.upsert_job(
        name="blog-check",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="Check blog updates",
        deliver=True,
        channel="feishu",
        to="oc_123",
    )
    second, second_status = service.upsert_job(
        name="blog-check",
        schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="Asia/Shanghai"),
        message="Check blog updates",
        deliver=True,
        channel="feishu",
        to="oc_123",
    )

    jobs = service.list_jobs(include_disabled=True)
    assert first_status == "created"
    assert second_status == "updated"
    assert len(jobs) == 1
    assert first.id == second.id
    assert second.schedule.kind == "cron"
    assert second.schedule.expr == "0 9 * * *"
    assert second.schedule.tz == "Asia/Shanghai"


def test_upsert_job_keeps_system_events_distinct_from_agent_turns(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    first, first_status = service.upsert_job(
        name="nightly-history-sync",
        schedule=CronSchedule(kind="cron", expr="0 4 * * *"),
        message="history_sync",
        payload_kind="system_event",
    )
    second, second_status = service.upsert_job(
        name="history-sync-chat",
        schedule=CronSchedule(kind="cron", expr="0 4 * * *"),
        message="history_sync",
    )

    jobs = service.list_jobs(include_disabled=True)
    assert first_status == "created"
    assert second_status == "created"
    assert len(jobs) == 2
    assert first.id != second.id


@pytest.mark.asyncio
async def test_running_service_honors_external_disable(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    called: list[str] = []

    async def on_job(job) -> None:
        called.append(job.id)

    service = CronService(store_path, on_job=on_job)
    job, status = service.upsert_job(
        name="external-disable",
        schedule=CronSchedule(kind="every", every_ms=200),
        message="hello",
    )
    assert status == "created"
    await service.start()
    try:
        external = CronService(store_path)
        updated = external.enable_job(job.id, enabled=False)
        assert updated is not None
        assert updated.enabled is False

        await asyncio.sleep(0.35)
        assert called == []
    finally:
        service.stop()
