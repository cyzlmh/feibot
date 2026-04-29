# feibot

> A lightweight multi-agent AI assistant framework with orchestration control plane.

---

## Overview

Feibot is a personal AI assistant framework designed for running multiple specialized agents with unified lifecycle management. It features:

- **Madame Control Plane**: Orchestrate multiple agent instances with credential pools, skill sharing, and lifecycle control
- **20+ LLM Providers**: Auto-detected by model name (Anthropic, OpenAI, DeepSeek, MiniMax, Gemini, etc.)
- **CLI Interactive Mode**: Local terminal chat without messaging platform dependency
- **General-purpose Tools**: Filesystem, shell execution, web search/fetch, scheduling
- **Skill-based Extensions**: Modular skill system for domain-specific workflows

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      Madame Control Plane                        │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │  Registry Store  │  Credential Pool  │  Skill Hub           ││
│  │  (agents.json)   │  (app credentials)│  (shared skills)     ││
│  └─────────────────────────────────────────────────────────────┘│
│                              │                                   │
│         ┌────────────────────┼────────────────────┐             │
│         ▼                    ▼                    ▼             │
│   ┌──────────┐         ┌──────────┐         ┌──────────┐       │
│   │ Agent A  │         │ Agent B  │         │ Agent C  │       │
│   │ (coder)  │         │ (chat)   │         │(research)│       │
│   └──────────┘         └──────────┘         └──────────┘       │
│         │                    │                    │             │
└─────────┼────────────────────┼────────────────────┼─────────────┘
          │                    │                    │
          ▼                    ▼                    ▼
┌─────────────────────────────────────────────────────────────────┐
│                       Agent Loop Core                            │
│  ┌─────────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐    │
│  │ Context     │ │ Session  │ │ Memory   │ │ Skill Loader │    │
│  │ Builder     │ │ Manager  │ │ Store    │ │              │    │
│  └─────────────┘ └──────────┘ └──────────┘ └──────────────┘    │
│                              │                                   │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │                     Tool Registry                          │ │
│  │  read │ write │ edit │ list │ find │ grep │ exec │ web │  │ │
│  │  cron │ message │ feishu_send_file                         │ │
│  └───────────────────────────────────────────────────────────┘ │
│                              │                                   │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │                    LLM Provider                            │ │
│  │  Anthropic │ OpenAI │ DeepSeek │ MiniMax │ Gemini │ ...   │ │
│  └───────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────┐
│                        Channels                                  │
│  ┌──────────────┐  ┌──────────────┐                             │
│  │ Feishu/Lark  │  │ CLI Terminal │                             │
│  │ WebSocket    │  │ Interactive  │                             │
│  └──────────────┘  └──────────────┘                             │
└─────────────────────────────────────────────────────────────────┘
```

---

## Core Features

### 1. Madame Multi-Agent Orchestration

Madame is the control plane for managing multiple specialized agent instances:

| Command | Description |
|---------|-------------|
| `/agent list` | List all managed agents (Markdown table) |
| `/agent create --name <id> --mode <agent|chat>` | Create new agent |
| `/agent start|stop|restart <id>` | Lifecycle control |
| `/agent restart all` | Bulk restart active agents |
| `/agent archive <id>` | Archive agent, backup workspace, release credential |
| `/agent pool list|add|remove` | Manage credential pool |
| `/agent cron <list|add|runs|remove|enable|disable|run>` | Scheduled jobs per agent |
| `/agent skills hub list|find|install|uninstall` | Shared skill library |
| `/skill list|show|add|remove|sync|clear` | Agent skill assignment |

**Agent Modes:**
- `agent`: Full tool access, skills enabled, memory enabled
- `chat`: Minimal tools (web_search, web_fetch only), no skills, no memory

**Credential Pool:** Dynamic pool of Feishu app credentials for agent allocation.

### 2. LLM Provider Ecosystem

Auto-detects provider by model name prefix. Supports **20+ providers**:

| Provider | Model Keywords | Default Base URL |
|----------|----------------|------------------|
| Anthropic | `anthropic`, `claude` | Native SDK |
| OpenAI | `openai`, `gpt` | SDK default |
| DeepSeek | `deepseek` | `https://api.deepseek.com` |
| Gemini | `gemini` | Google Generative AI API |
| Zhipu | `zhipu`, `glm` | `https://open.bigmodel.cn/api/paas/v4` |
| MiniMax | `minimax` | `https://api.minimax.io/v1` |
| Moonshot | `moonshot`, `kimi` | `https://api.moonshot.ai/v1` |
| DashScope | `qwen`, `dashscope` | Alibaba Cloud |
| Mistral | `mistral` | `https://api.mistral.ai/v1` |
| Groq | `groq` | `https://api.groq.com/openai/v1` |

**Gateways:**
- OpenRouter, AiHubMix, SiliconFlow, VolcEngine, BytePlus

**Local Deployment:**
- vLLM, Ollama, OpenVINO Model Server

**OAuth Providers:**
- OpenAI Codex (ChatGPT backend)
- GitHub Copilot

### 3. CLI Interactive Mode

Run agent locally without messaging platform:

```bash
# Interactive chat
uv run python -m feibot

# Single message
uv run python -m feibot -m "Explain Python async/await"

# With config
uv run python -m feibot --config ./config.json
```

Features: prompt_toolkit with history, streaming responses, Markdown rendering.

### 4. General-Purpose Tools

| Tool | Description |
|------|-------------|
| `read_file` | Read file contents (truncated at 128KB) |
| `write_file` | Write/create files (creates parent dirs) |
| `edit_file` | Replace text in existing file |
| `list_dir` | List directory contents |
| `find_file` | Find files by name (uses `fd`) |
| `grep_text` | Search text patterns (uses `rg`) |
| `exec` | Execute shell commands (timeout, host guards) |
| `web_search` | Tavily API search |
| `web_fetch` | Fetch and extract readable content from URL |
| `cron` | Schedule tasks (intervals, cron expressions) |
| `message` | Send messages via channel |
| `feishu_send_file` | Upload files to Feishu |

**Security Model:**
- `writableDirs`: Allowed write paths
- `allowedHosts`: Allowed remote hosts for SSH/SCP/RSYNC
- Files read-only by default; shell timeout configurable

### 5. Built-in Skills

| Skill | Description |
|-------|-------------|
| `memory` | Two-layer memory (MEMORY.md + HISTORY.md) |
| `cron` | Deterministic scheduled tasks |
| `heartbeat` | Ad-hoc review queue in HEARTBEAT.md |
| `github` | GitHub CLI (`gh`) for issues, PRs, CI |
| `agent-browser` | Browser automation |
| `tmux` | Remote-control tmux sessions |
| `summarize` | Summarize URLs, files, YouTube videos |
| `weather` | Weather info via wttr.in |
| `skill-creator` | Create new skills |
| `feishu-file-send` | Send files via Feishu |
| `feibot-ops` | Lifecycle operations via launchd/systemd |

### 6. Session & Memory

**Session Storage:**
- JSONL append-only archives: `sessions/YYYY/MM/DD/<session_id>.jsonl`
- Active session index for routing
- Message deduplication to prevent reprocessing

**Memory Layers:**
- `MEMORY.md`: Approved long-term facts (always loaded)
- `HISTORY.md`: Session summaries (grep-searchable)

### 7. Scheduling

**Cron Service:**
- `every`: Interval-based (e.g., every 30 minutes)
- `cron`: Cron expression with timezone
- `at`: One-shot timestamp

**Heartbeat Service:**
- Periodic review of HEARTBEAT.md (default: 30 min)
- LLM decides whether tasks are actionable

---

## Quick Start

### Install

```bash
uv sync
```

### Run CLI Mode

```bash
uv run python -m feibot --config ./config.json
```

### Run Gateway (Feishu)

```bash
uv run python -m feibot.gateway --config ./config.json
```

### Bootstrap Madame

```bash
uv run feibot madame init \
  --repo-dir ~/Projects/feibot \
  --madame-dir ~/madame \
  --app-id <MADAME_APP_ID> \
  --app-secret <MADAME_APP_SECRET> \
  --pool-slot "Agent1=<APP_ID>:<APP_SECRET>"
```

Then start Madame gateway:
```bash
~/madame/ops/manage.sh install
~/madame/ops/manage.sh start
```

---

## Configuration

### Minimal Config

```json
{
  "name": "my-agent",
  "paths": {
    "workspace": "./workspace",
    "sessions": "./sessions"
  },
  "agents": {
    "defaults": {
      "model": "openai/gpt-4o"
    }
  },
  "channels": {
    "send_progress": true,
    "send_tool_hints": true,
    "feishu": {
      "enabled": false,
      "app_id": "",
      "app_secret": "",
      "allow_from": []
    }
  },
  "providers": {
    "openai": {
      "api_key": "<OPENAI_API_KEY>"
    }
  },
  "tools": {
    "writableDirs": ["./workspace"],
    "allowedHosts": [],
    "exec": {
      "timeout": 300
    }
  },
  "madame": {
    "enabled": false
  }
}
```

### Madame Config

```json
{
  "madame": {
    "enabled": true,
    "runtime_id": "madame",
    "registry_path": "~/madame/agents_registry.json",
    "manage_script": "~/madame/ops/manage.sh",
    "base_dir_template": "~/madame/agents/{runtime_id}",
    "backup_dir": "~/madame/backups"
  }
}
```

---

## Agent Registry Structure

```json
{
  "version": 3,
  "credential_pool": [
    {
      "display_name": "Agent1",
      "app_id": "cli_xxx",
      "app_secret": "sec_xxx",
      "status": "available",
      "assigned_runtime_id": ""
    }
  ],
  "agents": [
    {
      "runtime_id": "agent1",
      "display_name": "Agent1",
      "mode": "agent",
      "role": "coder",
      "launchd_label": "ai.agent1.gateway",
      "config_path": "~/madame/agents/agent1/config.json",
      "workspace_path": "~/madame/agents/agent1/workspace",
      "skills": ["github", "cron"],
      "tool_policy": "default",
      "archived": false
    }
  ]
}
```

---

## In-Chat Commands

| Command | Description |
|---------|-------------|
| `/new` | Start fresh session |
| `/stop` | Cancel current task |
| `/go` | Resume paused task |
| `/fork [label]` | Subtask with context (creates Feishu group) |
| `/spawn [label]` | Subtask fresh context (creates Feishu group) |
| `/agent ...` | Madame control commands |
| `/skill ...` | Agent skill management |
| `/help` | Show available commands |
| `/chatid` | Show current chat ID |

---

## Project Stats

| Component | Lines |
|-----------|-------|
| Agent Loop Core | ~2000 |
| Madame Controller | ~1800 |
| Providers Registry | ~660 |
| Cron Service | ~670 |
| Feishu Channel | ~1300 |
| CLI Commands | ~1000 |

---

## License

MIT

Based on [nanobot](https://github.com/HKUDS/nanobot) framework.