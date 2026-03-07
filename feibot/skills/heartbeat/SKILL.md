---
name: heartbeat
description: Maintain HEARTBEAT.md as an ad-hoc review queue, not a fixed scheduler.
metadata: {"feibot":{"emoji":"💓"}}
---

# Heartbeat Skill

Use `HEARTBEAT.md` for open-ended review tasks that need periodic judgment.

## Intended Purpose

- Keep a lightweight backlog for "decide whether to act" workflows.
- Let heartbeat LLM checks decide `run` or `skip`.

## Not Intended For

- Fixed schedules like every 30 minutes or daily at 01:00.
- Repeating jobs that should be deterministic.

Those should be migrated to cron.

## Writing Guidelines

- Keep each item concise and action-oriented.
- Prefer checklist style (`- [ ] ...`) for pending items.
- Remove or archive items once migrated to cron or no longer needed.

## Migration Rule

When a heartbeat item has a clear schedule, move it to cron and replace the
heartbeat entry with a short migration note.
