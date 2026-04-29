---
name: cron
description: Schedule reminders and recurring tasks with the `cron` tool.
metadata: {"feibot":{"emoji":"⏰"}}
---

# Cron Skill

Use the `cron` tool for deterministic schedules.

## Use Cron When

- The user asks for fixed intervals (every N minutes/hours).
- The user asks for fixed calendar times (daily/weekly/monthly).
- The task should keep running without depending on `HEARTBEAT.md`.

## Do Not Use Cron When

- The task has no fixed schedule and needs periodic judgment.
- The task is a temporary backlog item better handled by heartbeat review.

## Common Calls

Add interval job:

```json
{
  "action": "add",
  "message": "Check something every 30 minutes",
  "every_seconds": 1800
}
```

Add cron expression with timezone:

```json
{
  "action": "add",
  "message": "Daily summary",
  "cron_expr": "0 9 * * *",
  "tz": "Asia/Shanghai"
}
```

Add one-time job:

```json
{
  "action": "add",
  "message": "One-time reminder",
  "at": "2026-03-05T14:30:00"
}
```

List and remove:

```json
{
  "action": "list"
}
```

```json
{
  "action": "remove",
  "job_id": "abc123"
}
```

## Time Conversion Notes

- Convert "every 20 minutes" to `every_seconds=1200`.
- Convert "weekdays at 5pm" to `cron_expr="0 17 * * 1-5"`.
- Use IANA timezone names for `tz` (for example `Asia/Shanghai`, `America/Los_Angeles`).
