# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync

# Run the bot (requires config.json)
uv run python -m feibot.gateway --config ./config.json

# Interactive CLI mode
uv run python -m feibot

# Run all tests
uv run pytest

# Run a single test file
uv run pytest tests/test_agent_loop.py

# Run a single test
uv run pytest tests/test_agent_loop.py::test_name

# Lint
uv run ruff check

# Fix lint issues
uv run ruff check --fix
```

## Architecture

Feibot is a Feishu (Lark) AI assistant framework. The core message flow:

```
Feishu WebSocket → InboundMessage → MessageBus (queue) → AgentLoop
  → LLMProvider → Tool execution → OutboundMessage → Feishu response
```

**Key components:**

- **`feibot/agent/loop.py`** — The main agent loop. Receives messages, builds context, calls LLM, executes tools, sends responses. This is the heaviest file (~81KB).
- **`feibot/channels/`** — Channel adapters. `feishu.py` handles the Feishu WebSocket long-connection; `manager.py` routes messages to/from the bus.
- **`feibot/bus/`** — Async message queue coordinating inbound (`InboundMessage`) and outbound (`OutboundMessage`) events between channels and the agent loop.
- **`feibot/providers/`** — LLM provider abstraction. Supports Anthropic, OpenAI-compatible endpoints (Azure, Groq, Dashscope, vLLM, Ollama, etc.), and OpenAI Codex OAuth. Provider is selected by matching the `model` string in config.
- **`feibot/agent/tools/`** — Tool execution layer: filesystem, shell exec, web search (Tavily), web fetch, Feishu APIs (wiki, bitable, drive), cron, and message tools. Write access is restricted to `writableDirs`; shell commands have a configurable timeout.
- **`feibot/skills/`** — Builtin skill definitions (markdown files). Each skill teaches the agent how to use a set of tools. Workspace skills override builtins.
- **`feibot/config/schema.py`** — Pydantic config schema. Config is a JSON file; `config.example.json` shows the full structure.
- **`feibot/session/`** — Per-session history stored as JSONL files, one per `channel:chat_id`. Handles rotation, deduplication, and recovery.
- **`feibot/madame/`** — Control plane for managing multiple agent instances: lifecycle (create/start/stop/restart/archive), credential pools, and launchd/systemd integration.
- **`feibot/cron/`** — Job scheduler supporting one-shot (`at`), interval (`every`), and cron-expression schedules. Jobs can run shell commands or trigger agent turns.
- **`feibot/heartbeat/`** — Periodically reads `HEARTBEAT.md`, asks the LLM whether any tasks are actionable, and executes them if so.

## Configuration

Config is required at startup. Copy `config.example.json` and fill in credentials. Key fields:

- `paths.workspace` — Where the agent writes files, skills, memory logs
- `paths.sessions` — Session history storage
- `agents.defaults.model` — Model string, e.g. `"anthropic/claude-opus-4-7"` or `"openai/gpt-4o"`
- `channels.feishu` — Feishu app credentials; set `allowFrom` to restrict which users can interact
- `tools.writableDirs` — Paths the agent is allowed to write to
- `tools.exec.timeout` — Shell command timeout in seconds
- `madame.enabled` — Enable the multi-agent orchestration control plane

## Testing

Tests use `pytest-asyncio` with `asyncio_mode = "auto"` — all async test functions are automatically treated as coroutines. Tests mock LLM providers; no real API keys are needed to run the test suite.

## In-chat Commands

The agent recognizes special slash commands: `/new` (fresh session), `/go` (resume interrupted task), `/stop` (cancel), `/fork` (subtask with context), `/spawn` (subtask fresh), `/agent` (Madame control: create, start, stop, list, archive, cron, pool).
