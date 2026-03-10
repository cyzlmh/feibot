---
name: memory
description: Two-layer memory system with grep-based recall.
always: true
---

# Memory

## Structure

- `memory/MEMORY.md` — Approved long-term facts (preferences, project context, relationships). Always loaded into your context.
- `memory/HISTORY.md` — Nightly-maintained session summaries keyed by `session_id`. NOT loaded into context. Search it only when the user asks about prior sessions or past events.
- `memory/REVIEW.md` — Recommendations from the nightly history sync. These are candidates only and are not approved memory.

## Search Past Events

```bash
grep -i "keyword" memory/HISTORY.md
```

Use the `exec` tool to run grep. Combine patterns: `grep -iE "meeting|deadline" memory/HISTORY.md`

## When to Update MEMORY.md

Write important facts only after explicit user approval using `edit_file` or `write_file`:
- User preferences ("I prefer dark mode")
- Project context ("The API uses OAuth2")
- Relationships ("Alice is the project lead")

Do not add:
- Transient bugs
- One-off tasks
- Session-local details that should live only in history

## History Sync

Session logs are archived in full and summarized into `HISTORY.md` by a nightly sync job. The sync job may write recommendations to `memory/REVIEW.md`, but it must not update `MEMORY.md` automatically.
