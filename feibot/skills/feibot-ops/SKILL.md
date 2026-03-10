---
name: feibot-ops
description: Standardize lifecycle operations for the local feibot gateway service via launchd. Supports multiple instances, listing all instances, and provides guidance on allowFrom configuration.
---

# Mybot Ops

## Overview

Use this skill to run repeatable and safe lifecycle actions for `feibot` through one script.
`launchd` is the primary supervisor (auto-start on login + keepalive).
Supports multiple instances with different configurations.

## Quick Start

```bash
/path/to/feibot-ops/scripts/manage.sh <command>
```

Commands:

- `list` - List all feibot instances (running and installed)
- `start` - Ensure launchd service is loaded and running
- `stop` - Stop launchd service
- `restart` - Restart gateway
- `status` - Show launchd/process/log status
- `logs [N]` - Tail last N log lines (default: 120)
- `install` - Write plist and bootstrap launchd service
- `uninstall` - Stop service, disable label, remove plist

## CLI Options

```bash
-c, --config FILE    Config file path
-r, --repo DIR       Repository directory
-l, --label LABEL    Launchd label (must be unique per instance)
-d, --run-dir DIR    Runtime directory
-h, --help           Show help
```

## Examples

```bash
# List all instances
./scripts/manage.sh list

# Default instance (auto-detected repo path + default config/run dirs)
./scripts/manage.sh restart

# Specify config file
./scripts/manage.sh -c /path/to/config.json status

# Different label for multi-instance
./scripts/manage.sh -l ai.feibot.prod -c /prod/config.json restart

# Full custom config
./scripts/manage.sh -r /path/to/feibot -c /path/to/config.json -l feibot-dev -d /path/to/run status
```

## Creating a New Instance

When creating a new instance:

1. **Create directory structure**:
   ```bash
   mkdir -p ~/botname/workspace ~/botname/sessions ~/botname/run
   ```

2. **Create config.json** with:
   - Set `name` to the bot's name (e.g., "suzy", "zoe")
   - Configure Feishu channel (appId, appSecret)
   - Set the model in `agents.defaults.model`
   - Use `allowFrom: []` for open access in testing, or a specific open_id list for restricted access (see below)

3. **Create AGENTS.md** in the workspace:
   ```markdown
   # Agent Instructions

   You are a helpful AI assistant. Your name is <Bot Name>. Be concise, accurate, and friendly.

   ## Guidelines

   - Always explain what you're doing before taking actions
   - Ask for clarification when the request is ambiguous
   - Use tools to help accomplish tasks
   - `memory/MEMORY.md` is approved global context; `memory/HISTORY.md` is the searchable session index
   ```

4. **Install and start**:
   ```bash
   ./scripts/manage.sh -l ai.botname.gateway -c ~/botname/config.json -r /path/to/feibot -d ~/botname/run install
   ```

5. **Configure allowFrom** (see next section)

## ⚠️ Important: allowFrom Configuration

The `allowFrom` field in the Feishu channel config is **special**:

1. It should contain Feishu user `open_id` values.
2. Empty list `[]` means **allow all**.
3. For restricted mode, collect IDs from logs after users send messages. IDs appear like:
   ```
   Access denied for sender ou_xxxxxxxxxxxx on channel feishu
   ```

### How to configure restricted allowFrom:

1. Start the bot and have the user send a message
2. Check the logs: `tail -f /path/to/run/gateway.log | grep "Access denied"`
3. Copy the `ou_xxx` ID from the log
4. Add it to the `allowFrom` list in config.json
5. Restart the bot

Example:
```json
"allowFrom": [
  "ou_6287aa1fb258625bd15f8f9789a04799",
  "ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
]
```

## Environment Contract

- Repository: `$REPO_DIR`
- Config: `$CONFIG_FILE`
- Runtime dir: `$RUN_DIR`
- Pid file: `$RUN_DIR/gateway.pid`
- Log file: `$RUN_DIR/gateway.log`
- launchd label: `$LAUNCHD_LABEL`
- launchd plist: `~/Library/LaunchAgents/$LAUNCHD_LABEL.plist`

## Rules

- Do not use `pkill -9` unless normal stop flow fails.
- Each instance must have a unique `LAUNCHD_LABEL`.
- After `restart`, confirm both launchd state and process existence.
- Remind users to choose `allowFrom` mode explicitly: `[]` (open) or a restricted allow-list.

## Output Contract

When responding to the user after running commands, always include:

1. action executed
2. resulting PID (if running)
3. launchd state
4. key log lines (startup or error)
5. evidence references: exact log file path + timestamp(s) used
