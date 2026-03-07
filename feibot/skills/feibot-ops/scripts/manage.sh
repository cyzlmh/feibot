#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"

# Default values (can be overridden via env vars or CLI options)
DEFAULT_REPO_DIR="${FEIBOT_REPO_DIR:-$REPO_ROOT}"
DEFAULT_CONFIG_FILE="${FEIBOT_CONFIG_FILE:-$DEFAULT_REPO_DIR/config.json}"
DEFAULT_RUN_DIR="${FEIBOT_RUN_DIR:-$HOME/.feibot/run}"
DEFAULT_LAUNCHD_LABEL="${FEIBOT_LAUNCHD_LABEL:-ai.feibot.gateway}"

usage() {
  cat <<EOF
Usage: $0 [options] <command>

Options:
  -c, --config FILE    Config file path (default: $DEFAULT_CONFIG_FILE)
  -r, --repo DIR       Repository directory (default: $DEFAULT_REPO_DIR)
  -l, --label LABEL    Launchd label (default: $DEFAULT_LAUNCHD_LABEL)
  -d, --run-dir DIR    Runtime directory (default: $DEFAULT_RUN_DIR)
  -h, --help           Show this help

Commands:
  start          Ensure launchd service is loaded and running
  stop           Stop launchd service in current login session
  restart        Restart gateway
  status         Show launchd/process/log status
  logs [N]       Tail last N log lines (default: 120)
  install        Write plist and bootstrap launchd service
  list           List all feibot instances (running and stopped)
  uninstall      Stop, remove plist service, disable label

Examples:
  $0 restart
  $0 -c /path/to/config.json restart
  $0 -l ai.feibot.gateway -c /path/to/config.json status
  $0 -r /path/to/feibot -c /path/to/config.json -l feibot-prod install
EOF
}

# Parse command-line options
while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--config)
      CONFIG_FILE="$2"
      shift 2
      ;;
    -r|--repo)
      REPO_DIR="$2"
      shift 2
      ;;
    -l|--label)
      LAUNCHD_LABEL="$2"
      shift 2
      ;;
    -d|--run-dir)
      RUN_DIR="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
    *)
      break
      ;;
  esac
done

# Set defaults if not overridden
REPO_DIR="${REPO_DIR:-$DEFAULT_REPO_DIR}"
CONFIG_FILE="${CONFIG_FILE:-$DEFAULT_CONFIG_FILE}"
RUN_DIR="${RUN_DIR:-$DEFAULT_RUN_DIR}"
LAUNCHD_LABEL="${LAUNCHD_LABEL:-$DEFAULT_LAUNCHD_LABEL}"

# Derived paths
ENV_FILE="$(dirname "$CONFIG_FILE")/.env"
LOG_FILE="$RUN_DIR/gateway.log"
PID_FILE="$RUN_DIR/gateway.pid"
FEIBOT_BIN="$REPO_DIR/.venv/bin/feibot"
LAUNCHD_DOMAIN="gui/${UID:-$(id -u)}"
LAUNCHD_TARGET="$LAUNCHD_DOMAIN/$LAUNCHD_LABEL"
PLIST_FILE="$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist"

ensure_paths() {
  mkdir -p "$RUN_DIR"
  mkdir -p "$(dirname "$PLIST_FILE")"
  touch "$LOG_FILE"
}

running_pid() {
  local pid
  pid="$(running_pids | tail -n1 || true)"
  echo "$pid" | tr -d '[:space:]'
}

running_pids() {
  ps -axo pid=,command= \
    | awk -v bin="$FEIBOT_BIN" -v cfg="$CONFIG_FILE" '
        {
          direct = ($2 == bin && index($0, "--config " cfg " gateway") > 0)
          via_py = ($2 ~ /python3$/ && index($0, " " bin " --config " cfg " gateway") > 0)
          if (direct || via_py) {
            print $1
          }
        }
      ' || true
}

write_pid_file() {
  local pid
  pid="$(running_pid)"
  if [[ -n "$pid" ]]; then
    echo "$pid" > "$PID_FILE"
  else
    rm -f "$PID_FILE"
  fi
}

launchd_loaded() {
  launchctl print "$LAUNCHD_TARGET" >/dev/null 2>&1
}

launchd_enabled_state() {
  local state
  state="$(launchctl print-disabled "$LAUNCHD_DOMAIN" 2>/dev/null \
    | awk -v label="\"$LAUNCHD_LABEL\"" '$1 == label {print $3; exit}')"
  if [[ -z "$state" ]]; then
    echo "unknown"
  else
    echo "$state"
  fi
}

launchd_state() {
  launchctl print "$LAUNCHD_TARGET" 2>/dev/null \
    | awk -F' = ' '/state = / {print $2; exit}'
}

xml_escape() {
  local s="${1:-}"
  s="${s//&/&amp;}"
  s="${s//</&lt;}"
  s="${s//>/&gt;}"
  printf '%s' "$s"
}

write_plist() {
  local launch_cmd
  local launch_cmd_xml

  launch_cmd="cd \"$REPO_DIR\" && export PYTHONUNBUFFERED=1 && if [[ -f \"$ENV_FILE\" ]]; then set -a && source \"$ENV_FILE\" && set +a; fi && exec \"$FEIBOT_BIN\" --config \"$CONFIG_FILE\" gateway"
  launch_cmd_xml="$(xml_escape "$launch_cmd")"

  cat > "$PLIST_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LAUNCHD_LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>$launch_cmd_xml</string>
  </array>

  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>60</integer>

  <key>WorkingDirectory</key>
  <string>$REPO_DIR</string>
  <key>StandardOutPath</key>
  <string>$LOG_FILE</string>
  <key>StandardErrorPath</key>
  <string>$LOG_FILE</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>$HOME</string>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
EOF

  plutil -lint "$PLIST_FILE" >/dev/null
}

bootstrap_launchd() {
  launchctl bootstrap "$LAUNCHD_DOMAIN" "$PLIST_FILE"
}

cmd_install() {
  ensure_paths
  if [[ ! -x "$FEIBOT_BIN" ]]; then
    echo "ERROR: feibot executable not found: $FEIBOT_BIN" >&2
    exit 1
  fi

  write_plist
  launchctl enable "$LAUNCHD_TARGET" >/dev/null 2>&1 || true
  if launchd_loaded; then
    launchctl bootout "$LAUNCHD_TARGET" >/dev/null 2>&1 || true
  fi
  bootstrap_launchd

  sleep 2
  write_pid_file
  local pid
  pid="$(running_pid)"
  echo "Installed: label=$LAUNCHD_LABEL plist=$PLIST_FILE"
  if [[ -n "$pid" ]]; then
    echo "Running: pid=$pid"
  else
    echo "WARNING: service loaded but process not found yet."
  fi
}

cmd_start() {
  ensure_paths
  if [[ ! -x "$FEIBOT_BIN" ]]; then
    echo "ERROR: feibot executable not found: $FEIBOT_BIN" >&2
    exit 1
  fi

  write_plist
  launchctl enable "$LAUNCHD_TARGET" >/dev/null 2>&1 || true

  local pid
  pid="$(running_pid)"
  if [[ -n "$pid" ]] && launchd_loaded; then
    write_pid_file
    echo "Already running: pid=$pid launchd=$LAUNCHD_LABEL"
    return 0
  fi

  if launchd_loaded; then
    launchctl kickstart "$LAUNCHD_TARGET" >/dev/null 2>&1 || true
  else
    bootstrap_launchd
  fi

  sleep 2
  write_pid_file
  pid="$(running_pid)"
  if [[ -z "$pid" ]]; then
    echo "ERROR: start failed; process not found after launch." >&2
    tail -n 80 "$LOG_FILE" || true
    exit 1
  fi

  echo "Started: pid=$pid launchd=$LAUNCHD_LABEL"
}

cmd_stop() {
  ensure_paths
  local pids

  if launchd_loaded; then
    launchctl bootout "$LAUNCHD_TARGET" >/dev/null 2>&1 || true
  fi

  # Graceful terminate loop to handle stragglers.
  for _ in 1 2 3; do
    pids="$(running_pids)"
    [[ -n "$pids" ]] || break
    while IFS= read -r pid; do
      [[ -n "$pid" ]] || continue
      kill "$pid" 2>/dev/null || true
    done <<< "$pids"
    sleep 1
  done

  # Force-kill any remaining processes.
  pids="$(running_pids)"
  if [[ -n "$pids" ]]; then
    while IFS= read -r pid; do
      [[ -n "$pid" ]] || continue
      kill -9 "$pid" 2>/dev/null || true
    done <<< "$pids"
    sleep 1
  fi

  write_pid_file
  echo "Stopped."
}

cmd_restart() {
  ensure_paths
  if [[ ! -x "$FEIBOT_BIN" ]]; then
    echo "ERROR: feibot executable not found: $FEIBOT_BIN" >&2
    exit 1
  fi

  write_plist
  launchctl enable "$LAUNCHD_TARGET" >/dev/null 2>&1 || true

  if launchd_loaded; then
    launchctl kickstart -k "$LAUNCHD_TARGET"
  else
    bootstrap_launchd
  fi

  sleep 2
  write_pid_file
  local pid
  pid="$(running_pid)"
  if [[ -z "$pid" ]]; then
    echo "ERROR: restart failed; process not found after launch." >&2
    tail -n 80 "$LOG_FILE" || true
    exit 1
  fi

  echo "Restarted: pid=$pid launchd=$LAUNCHD_LABEL"
}

cmd_uninstall() {
  ensure_paths

  cmd_stop
  launchctl disable "$LAUNCHD_TARGET" >/dev/null 2>&1 || true
  rm -f "$PLIST_FILE"

  echo "Uninstalled: label=$LAUNCHD_LABEL plist_removed=$PLIST_FILE"
}

cmd_status() {
  ensure_paths
  write_pid_file
  local enabled_state
  enabled_state="$(launchd_enabled_state)"

  if [[ -f "$PLIST_FILE" ]]; then
    echo "plist: installed ($PLIST_FILE)"
  else
    echo "plist: missing ($PLIST_FILE)"
  fi

  echo "launchd_enabled: $enabled_state"
  if launchd_loaded; then
    local state
    state="$(launchd_state)"
    if [[ -z "$state" ]]; then
      state="unknown"
    fi
    echo "launchd: loaded ($LAUNCHD_LABEL state=$state)"
  else
    echo "launchd: not loaded ($LAUNCHD_LABEL)"
  fi

  local pid
  pid="$(running_pid)"

  if [[ -n "$pid" ]]; then
    echo "process: running (pid=$pid)"
    ps -p "$pid" -o pid=,ppid=,stat=,command= || true
  else
    echo "process: stopped"
  fi

  if [[ -f "$PID_FILE" ]]; then
    echo "pid_file: $(cat "$PID_FILE")"
  else
    echo "pid_file: missing"
  fi

  echo "log_tail:"
  tail -n 20 "$LOG_FILE" || true
}

cmd_logs() {
  ensure_paths
  local n="${1:-120}"
  tail -n "$n" "$LOG_FILE"
}

cmd_list() {
  echo "=== Mybot Instances ==="
  echo ""
  
  # List all launchd services matching feibot pattern
  echo "Launchd services:"
  launchctl list 2>/dev/null | grep -E "ai\.(feibot|suzy|zoe|zebra)" | while read pid status label; do
    if [[ "$pid" == "-" ]]; then
      echo "  $label: stopped"
    else
      echo "  $label: running (pid=$pid)"
    fi
  done
  
  echo ""
  echo "Config files found:"
  for dir in ~/bot-ws ~/suzy ~/zoe ~/zoezebra; do
    if [[ -f "$dir/config.json" ]]; then
      echo "  $dir/config.json"
    fi
  done
}
main() {
  if [[ $# -lt 1 ]]; then
    usage
    exit 1
  fi

  local cmd="$1"
  shift

  case "$cmd" in
    start)
      cmd_start
      ;;
    stop)
      cmd_stop
      ;;
    restart)
      cmd_restart
      ;;
    status)
      cmd_status
      ;;
    logs)
      cmd_logs "${1:-120}"
      ;;
    install)
      cmd_install
      ;;
    list)
      cmd_list
      ;;
    uninstall)
      cmd_uninstall
      ;;
    *)
      echo "Unknown command: $cmd" >&2
      usage
      exit 1
      ;;
  esac
}

main "$@"
