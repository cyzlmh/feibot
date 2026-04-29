"""Cron service for scheduling agent tasks."""

import asyncio
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine, Literal

from loguru import logger

from feibot.cron.types import (
    CronExecutionResult,
    CronJob,
    CronJobState,
    CronPayload,
    CronSchedule,
    CronStore,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _compute_next_run(schedule: CronSchedule, now_ms: int) -> int | None:
    """Compute next run time in ms."""
    if schedule.kind == "at":
        return schedule.at_ms if schedule.at_ms and schedule.at_ms > now_ms else None

    if schedule.kind == "every":
        if not schedule.every_ms or schedule.every_ms <= 0:
            return None
        return now_ms + schedule.every_ms

    if schedule.kind == "cron" and schedule.expr:
        try:
            from zoneinfo import ZoneInfo

            from croniter import croniter

            base_time = now_ms / 1000
            tz = ZoneInfo(schedule.tz) if schedule.tz else datetime.now().astimezone().tzinfo
            base_dt = datetime.fromtimestamp(base_time, tz=tz)
            cron = croniter(schedule.expr, base_dt)
            next_dt = cron.get_next(datetime)
            return int(next_dt.timestamp() * 1000)
        except Exception:
            return None

    return None


def _validate_schedule_for_add(schedule: CronSchedule) -> None:
    """Validate schedule fields that would otherwise create non-runnable jobs."""
    if schedule.tz and schedule.kind != "cron":
        raise ValueError("tz can only be used with cron schedules")

    if schedule.kind == "cron" and schedule.tz:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(schedule.tz)
        except Exception:
            raise ValueError(f"unknown timezone '{schedule.tz}'") from None


def _normalize_message_for_identity(message: str) -> str:
    """Normalize user message for exact-idempotent identity matching."""
    return " ".join((message or "").split()).casefold()


def _validate_payload_for_add(
    *,
    payload_kind: Literal["system_event", "agent_turn", "exec"],
    message: str,
    command: str | None,
    working_dir: str | None,
) -> None:
    if payload_kind == "exec":
        if not str(command or "").strip():
            raise ValueError("exec payload requires command")
        if message:
            raise ValueError("message must be empty for exec payloads")
        return

    if working_dir:
        raise ValueError("working_dir can only be used with exec payloads")
    if not message:
        raise ValueError(f"{payload_kind} payload requires message")


def _schedule_equals(a: CronSchedule, b: CronSchedule) -> bool:
    return (
        a.kind == b.kind
        and a.at_ms == b.at_ms
        and a.every_ms == b.every_ms
        and a.expr == b.expr
        and a.tz == b.tz
    )


def _normalize_notify_policy(value: str | None) -> Literal["always", "changes_only", "digest"]:
    raw = str(value or "").strip().lower()
    if raw in {"always", "changes_only", "digest"}:
        return raw
    return "changes_only"


def _normalize_run_status(value: str | None) -> Literal["ok", "error", "skipped"] | None:
    raw = str(value or "").strip().lower()
    if raw in {"ok", "error", "skipped"}:
        return raw
    return None


def _normalize_business_status(value: str | None) -> Literal["changed", "no_change", "error", "n_a"] | None:
    raw = str(value or "").strip().lower()
    if raw in {"changed", "no_change", "error", "n_a"}:
        return raw
    return None


def _normalize_delivery_status(
    value: str | None,
) -> Literal["delivered", "not_delivered", "not_requested", "unknown"] | None:
    raw = str(value or "").strip().lower()
    if raw in {"delivered", "not_delivered", "not_requested", "unknown"}:
        return raw
    return None


def _as_non_negative_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


class CronService:
    """Service for managing and executing scheduled jobs."""

    def __init__(
        self,
        store_path: Path,
        on_job: Callable[[CronJob], Coroutine[Any, Any, CronExecutionResult]] | None = None,
    ):
        self.store_path = store_path
        self.on_job = on_job
        self._store: CronStore | None = None
        self._last_mtime: float = 0.0
        self._timer_task: asyncio.Task | None = None
        self._running = False

    @property
    def _runs_dir(self) -> Path:
        return self.store_path.parent / "runs"

    def _load_store(self) -> CronStore:
        """Load jobs from disk. Reloads automatically if file was modified externally."""
        if self._store and self.store_path.exists():
            mtime = self.store_path.stat().st_mtime
            if mtime != self._last_mtime:
                logger.info("Cron: jobs.json modified externally, reloading")
                self._store = None
        if self._store:
            return self._store

        if self.store_path.exists():
            try:
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                jobs = []
                for j in data.get("jobs", []):
                    payload = j.get("payload", {})
                    state = j.get("state", {})
                    notify_policy = _normalize_notify_policy(payload.get("notifyPolicy"))
                    if "notifyPolicy" not in payload and bool(payload.get("deliver", False)):
                        notify_policy = "always"
                    jobs.append(
                        CronJob(
                            id=j["id"],
                            name=j["name"],
                            enabled=j.get("enabled", True),
                            schedule=CronSchedule(
                                kind=j["schedule"]["kind"],
                                at_ms=j["schedule"].get("atMs"),
                                every_ms=j["schedule"].get("everyMs"),
                                expr=j["schedule"].get("expr"),
                                tz=j["schedule"].get("tz"),
                            ),
                            payload=CronPayload(
                                kind=payload.get("kind", "agent_turn"),
                                message=payload.get("message", ""),
                                command=payload.get("command"),
                                working_dir=payload.get("workingDir"),
                                notify_policy=notify_policy,
                                notify_on_error=bool(payload.get("notifyOnError", True)),
                                channel=payload.get("channel"),
                                to=payload.get("to"),
                            ),
                            state=CronJobState(
                                running_at_ms=state.get("runningAtMs"),
                                next_run_at_ms=state.get("nextRunAtMs"),
                                last_run_at_ms=state.get("lastRunAtMs"),
                                last_duration_ms=state.get("lastDurationMs"),
                                run_status=_normalize_run_status(state.get("runStatus") or state.get("lastStatus")),
                                business_status=_normalize_business_status(state.get("businessStatus")),
                                delivery_status=_normalize_delivery_status(state.get("deliveryStatus")),
                                last_error=state.get("lastError"),
                                last_delivery_error=state.get("lastDeliveryError"),
                                last_fingerprint=state.get("lastFingerprint"),
                                consecutive_errors=_as_non_negative_int(state.get("consecutiveErrors"), 0),
                            ),
                            created_at_ms=j.get("createdAtMs", 0),
                            updated_at_ms=j.get("updatedAtMs", 0),
                            delete_after_run=j.get("deleteAfterRun", False),
                        )
                    )
                self._store = CronStore(jobs=jobs)
            except Exception as e:
                logger.warning("Failed to load cron store: {}", e)
                self._store = CronStore()
        else:
            self._store = CronStore()

        return self._store

    def _save_store(self) -> None:
        """Save jobs to disk."""
        if not self._store:
            return

        self.store_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "version": self._store.version,
            "jobs": [
                {
                    "id": j.id,
                    "name": j.name,
                    "enabled": j.enabled,
                    "schedule": {
                        "kind": j.schedule.kind,
                        "atMs": j.schedule.at_ms,
                        "everyMs": j.schedule.every_ms,
                        "expr": j.schedule.expr,
                        "tz": j.schedule.tz,
                    },
                    "payload": {
                        "kind": j.payload.kind,
                        "message": j.payload.message,
                        "command": j.payload.command,
                        "workingDir": j.payload.working_dir,
                        "notifyPolicy": j.payload.notify_policy,
                        "notifyOnError": j.payload.notify_on_error,
                        "channel": j.payload.channel,
                        "to": j.payload.to,
                    },
                    "state": {
                        "runningAtMs": j.state.running_at_ms,
                        "nextRunAtMs": j.state.next_run_at_ms,
                        "lastRunAtMs": j.state.last_run_at_ms,
                        "lastDurationMs": j.state.last_duration_ms,
                        "runStatus": j.state.run_status,
                        "businessStatus": j.state.business_status,
                        "deliveryStatus": j.state.delivery_status,
                        "lastError": j.state.last_error,
                        "lastDeliveryError": j.state.last_delivery_error,
                        "lastFingerprint": j.state.last_fingerprint,
                        "consecutiveErrors": j.state.consecutive_errors,
                    },
                    "createdAtMs": j.created_at_ms,
                    "updatedAtMs": j.updated_at_ms,
                    "deleteAfterRun": j.delete_after_run,
                }
                for j in self._store.jobs
            ],
        }

        self.store_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        self._last_mtime = self.store_path.stat().st_mtime

    def _append_run_log(
        self,
        *,
        job: CronJob,
        result: CronExecutionResult,
        start_ms: int,
        end_ms: int,
    ) -> None:
        entry = {
            "ts": end_ms,
            "jobId": job.id,
            "action": "finished",
            "runStatus": result.run_status,
            "businessStatus": result.business_status,
            "deliveryStatus": result.delivery_status,
            "error": result.error,
            "deliveryError": result.delivery_error,
            "summary": result.summary,
            "fingerprint": result.fingerprint,
            "runAtMs": start_ms,
            "durationMs": max(0, end_ms - start_ms),
            "nextRunAtMs": job.state.next_run_at_ms,
        }
        path = self._runs_dir / f"{job.id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def start(self) -> None:
        """Start the cron service."""
        self._running = True
        self._load_store()
        self._recompute_next_runs()
        self._save_store()
        self._arm_timer()
        logger.info("Cron service started with {} jobs", len(self._store.jobs if self._store else []))

    def stop(self) -> None:
        """Stop the cron service."""
        self._running = False
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None

    def _recompute_next_runs(self) -> None:
        """Recompute next run times for all enabled jobs."""
        if not self._store:
            return
        now = _now_ms()
        for job in self._store.jobs:
            if job.enabled:
                job.state.next_run_at_ms = _compute_next_run(job.schedule, now)

    def _get_next_wake_ms(self) -> int | None:
        """Get the earliest next run time across all jobs."""
        if not self._store:
            return None
        times = [j.state.next_run_at_ms for j in self._store.jobs if j.enabled and j.state.next_run_at_ms]
        return min(times) if times else None

    def _arm_timer(self) -> None:
        """Schedule the next timer tick."""
        if self._timer_task:
            self._timer_task.cancel()

        next_wake = self._get_next_wake_ms()
        if not next_wake or not self._running:
            return

        delay_ms = max(0, next_wake - _now_ms())
        delay_s = delay_ms / 1000

        async def tick():
            await asyncio.sleep(delay_s)
            if self._running:
                await self._on_timer()

        self._timer_task = asyncio.create_task(tick())

    async def _on_timer(self) -> None:
        """Handle timer tick - run due jobs."""
        self._load_store()
        if not self._store:
            return

        now = _now_ms()
        due_jobs = [
            j
            for j in self._store.jobs
            if j.enabled and j.state.next_run_at_ms and now >= j.state.next_run_at_ms
        ]

        for job in due_jobs:
            await self._execute_job(job)

        self._save_store()
        self._arm_timer()

    async def _execute_job(self, job: CronJob) -> None:
        """Execute a single job."""
        start_ms = _now_ms()
        job.state.running_at_ms = start_ms
        job.state.last_error = None
        job.state.last_delivery_error = None
        logger.info("Cron: executing job '{}' ({})", job.name, job.id)

        result: CronExecutionResult
        try:
            if self.on_job:
                result = await self.on_job(job)
                if not isinstance(result, CronExecutionResult):
                    raise TypeError("cron callback must return CronExecutionResult")
            else:
                result = CronExecutionResult(
                    run_status="skipped",
                    business_status="n_a",
                    delivery_status="not_requested",
                    summary="No cron execution callback configured.",
                )
        except Exception as e:
            logger.error("Cron: job '{}' failed: {}", job.name, e)
            result = CronExecutionResult(
                run_status="error",
                business_status="error",
                delivery_status="not_requested",
                error=str(e),
            )

        end_ms = _now_ms()
        job.state.running_at_ms = None
        job.state.last_run_at_ms = start_ms
        job.state.last_duration_ms = max(0, end_ms - start_ms)
        job.state.run_status = result.run_status
        job.state.business_status = result.business_status
        job.state.delivery_status = result.delivery_status
        job.state.last_error = result.error
        job.state.last_delivery_error = result.delivery_error
        job.state.last_fingerprint = result.fingerprint
        if result.run_status == "error":
            job.state.consecutive_errors = max(0, job.state.consecutive_errors) + 1
        else:
            job.state.consecutive_errors = 0
        job.updated_at_ms = end_ms

        if job.schedule.kind == "at":
            if job.delete_after_run:
                self._store.jobs = [j for j in self._store.jobs if j.id != job.id]
            else:
                job.enabled = False
                job.state.next_run_at_ms = None
        else:
            job.state.next_run_at_ms = _compute_next_run(job.schedule, end_ms)

        self._append_run_log(job=job, result=result, start_ms=start_ms, end_ms=end_ms)

    # ========== Public API ==========

    def list_jobs(self, include_disabled: bool = False) -> list[CronJob]:
        """List all jobs."""
        store = self._load_store()
        jobs = store.jobs if include_disabled else [j for j in store.jobs if j.enabled]
        return sorted(jobs, key=lambda j: j.state.next_run_at_ms or float("inf"))

    def list_runs(self, job_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """List recent run log entries for one job (newest first)."""
        path = self._runs_dir / f"{job_id}.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        out: list[dict[str, Any]] = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
            if len(out) >= max(1, limit):
                break
        return out

    @staticmethod
    def _identity_key(
        *,
        kind: str,
        message: str,
        command: str | None,
        working_dir: str | None,
        channel: str | None,
        to: str | None,
    ) -> tuple[str, str, str, str, str]:
        identity_value = command if str(kind or "") == "exec" else message
        return (
            str(kind or "agent_turn"),
            _normalize_message_for_identity(identity_value or ""),
            str(working_dir or ""),
            str(channel or ""),
            str(to or ""),
        )

    def upsert_job(
        self,
        name: str,
        schedule: CronSchedule,
        message: str,
        payload_kind: Literal["system_event", "agent_turn", "exec"] = "agent_turn",
        notify_policy: Literal["always", "changes_only", "digest"] = "changes_only",
        notify_on_error: bool = True,
        channel: str | None = None,
        to: str | None = None,
        delete_after_run: bool = False,
        command: str | None = None,
        working_dir: str | None = None,
    ) -> tuple[CronJob, Literal["created", "updated", "unchanged"]]:
        """
        Create or update a logical job identity.

        Identity key: normalized message + channel + to.
        If identity exists, keep a single job and update schedule instead of
        creating duplicates.
        """
        store = self._load_store()
        _validate_schedule_for_add(schedule)
        normalized_message = str(message or "").strip()
        normalized_command = str(command or "").strip() or None
        normalized_working_dir = str(working_dir or "").strip() or None
        _validate_payload_for_add(
            payload_kind=payload_kind,
            message=normalized_message,
            command=normalized_command,
            working_dir=normalized_working_dir,
        )
        now = _now_ms()
        notify_policy = _normalize_notify_policy(notify_policy)

        target_identity = self._identity_key(
            kind=payload_kind,
            message=normalized_message,
            command=normalized_command,
            working_dir=normalized_working_dir,
            channel=channel,
            to=to,
        )
        matched = [
            job
            for job in store.jobs
            if self._identity_key(
                kind=job.payload.kind,
                message=job.payload.message,
                command=job.payload.command,
                working_dir=job.payload.working_dir,
                channel=job.payload.channel,
                to=job.payload.to,
            )
            == target_identity
        ]

        if matched:
            keep = matched[0]

            collapsed_duplicates = False
            if len(matched) > 1:
                extra_ids = {job.id for job in matched[1:]}
                store.jobs = [job for job in store.jobs if job.id not in extra_ids]
                collapsed_duplicates = True
                logger.warning(
                    "Cron: collapsed {} duplicate job(s) for identity '{}'",
                    len(extra_ids),
                    target_identity[0],
                )

            needs_update = (
                not keep.enabled
                or keep.name != name
                or keep.payload.notify_policy != notify_policy
                or keep.payload.notify_on_error != notify_on_error
                or keep.payload.channel != channel
                or keep.payload.to != to
                or keep.delete_after_run != delete_after_run
                or not _schedule_equals(keep.schedule, schedule)
            )

            if needs_update:
                keep.name = name
                keep.enabled = True
                keep.schedule = schedule
                keep.payload = CronPayload(
                    kind=payload_kind,
                    message=normalized_message,
                    command=normalized_command,
                    working_dir=normalized_working_dir,
                    notify_policy=notify_policy,
                    notify_on_error=notify_on_error,
                    channel=channel,
                    to=to,
                )
                keep.delete_after_run = delete_after_run
                keep.updated_at_ms = now
                keep.state.next_run_at_ms = _compute_next_run(schedule, now)
                self._save_store()
                self._arm_timer()
                logger.info("Cron: updated job '{}' ({})", keep.name, keep.id)
                return keep, "updated"

            if collapsed_duplicates:
                self._save_store()
                self._arm_timer()
            logger.info("Cron: unchanged existing job '{}' ({})", keep.name, keep.id)
            return keep, "unchanged"

        job = CronJob(
            id=str(uuid.uuid4())[:8],
            name=name,
            enabled=True,
            schedule=schedule,
            payload=CronPayload(
                kind=payload_kind,
                message=normalized_message,
                command=normalized_command,
                working_dir=normalized_working_dir,
                notify_policy=notify_policy,
                notify_on_error=notify_on_error,
                channel=channel,
                to=to,
            ),
            state=CronJobState(next_run_at_ms=_compute_next_run(schedule, now)),
            created_at_ms=now,
            updated_at_ms=now,
            delete_after_run=delete_after_run,
        )
        store.jobs.append(job)
        self._save_store()
        self._arm_timer()
        return job, "created"

    def remove_job(self, job_id: str) -> bool:
        """Remove a job by ID."""
        store = self._load_store()
        before = len(store.jobs)
        store.jobs = [j for j in store.jobs if j.id != job_id]
        removed = len(store.jobs) < before

        if removed:
            self._save_store()
            self._arm_timer()
            logger.info("Cron: removed job {}", job_id)

        return removed

    def enable_job(self, job_id: str, enabled: bool = True) -> CronJob | None:
        """Enable or disable a job."""
        store = self._load_store()
        for job in store.jobs:
            if job.id == job_id:
                job.enabled = enabled
                job.updated_at_ms = _now_ms()
                if enabled:
                    job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())
                else:
                    job.state.next_run_at_ms = None
                self._save_store()
                self._arm_timer()
                return job
        return None

    async def run_job(self, job_id: str, force: bool = False) -> bool:
        """Manually run a job."""
        store = self._load_store()
        for job in store.jobs:
            if job.id == job_id:
                if not force and not job.enabled:
                    return False
                await self._execute_job(job)
                self._save_store()
                self._arm_timer()
                return True
        return False

    def status(self) -> dict:
        """Get service status."""
        store = self._load_store()
        running_count = sum(1 for j in store.jobs if j.state.running_at_ms)
        return {
            "enabled": self._running,
            "jobs": len(store.jobs),
            "running_jobs": running_count,
            "next_wake_at_ms": self._get_next_wake_ms(),
        }
