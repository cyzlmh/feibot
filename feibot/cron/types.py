"""Cron types."""

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class CronSchedule:
    """Schedule definition for a cron job."""

    kind: Literal["at", "every", "cron"]
    # For "at": timestamp in ms
    at_ms: int | None = None
    # For "every": interval in ms
    every_ms: int | None = None
    # For "cron": cron expression (e.g. "0 9 * * *")
    expr: str | None = None
    # Timezone for cron expressions
    tz: str | None = None


@dataclass
class CronPayload:
    """What to do when the job runs."""

    kind: Literal["system_event", "agent_turn", "exec"] = "agent_turn"
    message: str = ""
    command: str | None = None
    working_dir: str | None = None
    # Delivery policy for user-facing notifications
    notify_policy: Literal["always", "changes_only", "digest"] = "changes_only"
    notify_on_error: bool = True
    channel: str | None = None  # e.g. "feishu"
    to: str | None = None  # e.g. Feishu chat_id


@dataclass
class CronJobState:
    """Runtime state of a job."""

    running_at_ms: int | None = None
    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_duration_ms: int | None = None
    run_status: Literal["ok", "error", "skipped"] | None = None
    business_status: Literal["changed", "no_change", "error", "n_a"] | None = None
    delivery_status: Literal["delivered", "not_delivered", "not_requested", "unknown"] | None = None
    last_error: str | None = None
    last_delivery_error: str | None = None
    last_fingerprint: str | None = None
    consecutive_errors: int = 0


@dataclass
class CronExecutionResult:
    """Structured result returned by cron execution callback."""

    run_status: Literal["ok", "error", "skipped"] = "ok"
    business_status: Literal["changed", "no_change", "error", "n_a"] = "n_a"
    delivery_status: Literal["delivered", "not_delivered", "not_requested", "unknown"] = "not_requested"
    summary: str | None = None
    user_message: str | None = None
    fingerprint: str | None = None
    error: str | None = None
    delivery_error: str | None = None


@dataclass
class CronJob:
    """A scheduled job."""

    id: str
    name: str
    enabled: bool = True
    schedule: CronSchedule = field(default_factory=lambda: CronSchedule(kind="every"))
    payload: CronPayload = field(default_factory=CronPayload)
    state: CronJobState = field(default_factory=CronJobState)
    created_at_ms: int = 0
    updated_at_ms: int = 0
    delete_after_run: bool = False


@dataclass
class CronStore:
    """Persistent store for cron jobs."""

    version: int = 1
    jobs: list[CronJob] = field(default_factory=list)
