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
LOG_FILE="$RUN_DIR/gateway.log"
PID_FILE="$RUN_DIR/gateway.pid"
PYTHON_BIN="$REPO_DIR/.venv/bin/python"
LAUNCHD_DOMAIN="gui/${UID:-$(id -u)}"
LAUNCHD_TARGET="$LAUNCHD_DOMAIN/$LAUNCHD_LABEL"
PLIST_FILE="$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist"

INSTALL_HINT="$0 -r \"$REPO_DIR\" -l \"$LAUNCHD_LABEL\" -c \"$CONFIG_FILE\" -d \"$RUN_DIR\" install"

ensure_write_paths() {
  mkdir -p "$RUN_DIR"
  mkdir -p "$(dirname "$PLIST_FILE")"
}

require_python_bin() {
  if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "ERROR: Python executable not found: $PYTHON_BIN" >&2
    exit 1
  fi
}

running_pid() {
  local pid
  pid="$(running_pids | tail -n1 || true)"
  echo "$pid" | tr -d '[:space:]'
}

running_pids() {
  ps -axo pid=,command= \
    | awk -v py="$PYTHON_BIN" -v cfg="$CONFIG_FILE" '
        {
          direct = ($2 == py && index($0, "-m feibot.gateway --config " cfg) > 0)
          via_py = ($2 ~ /(^|\/)python([0-9]+(\.[0-9]+)*)?$/ && index($0, " -m feibot.gateway --config " cfg) > 0)
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

launchd_print() {
  launchctl print "$LAUNCHD_TARGET" 2>/dev/null || true
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
  local tmp_plist

  launch_cmd="cd \"$REPO_DIR\" && export PYTHONUNBUFFERED=1 && exec \"$PYTHON_BIN\" -m feibot.gateway --config \"$CONFIG_FILE\""
  launch_cmd_xml="$(xml_escape "$launch_cmd")"
  tmp_plist="$(mktemp "${PLIST_FILE}.tmp.XXXXXX")"
  trap 'rm -f "$tmp_plist"' RETURN

  cat > "$tmp_plist" <<EOF
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

  plutil -lint "$tmp_plist" >/dev/null
  mv "$tmp_plist" "$PLIST_FILE"
  trap - RETURN
}

bootstrap_launchd() {
  if launchd_loaded; then
    echo "ERROR: launchd target is still loaded before bootstrap: $LAUNCHD_TARGET" >&2
    exit 1
  fi
  launchctl bootstrap "$LAUNCHD_DOMAIN" "$PLIST_FILE"
}

wait_until_unloaded() {
  local retries="${1:-30}"
  local delay="${2:-0.2}"
  local i

  for ((i=0; i<retries; i++)); do
    if ! launchd_loaded; then
      return 0
    fi
    sleep "$delay"
  done
  return 1
}

unload_launchd() {
  if ! launchd_loaded; then
    return 0
  fi

  launchctl bootout "$LAUNCHD_TARGET" >/dev/null 2>&1 || true
  if ! wait_until_unloaded 30 0.2; then
    echo "ERROR: launchd target did not unload cleanly: $LAUNCHD_TARGET" >&2
    launchctl print "$LAUNCHD_TARGET" 2>/dev/null | sed -n '1,80p' >&2 || true
    exit 1
  fi
}

running_inside_target_job() {
  if [[ "${XPC_SERVICE_NAME:-}" == "$LAUNCHD_LABEL" ]]; then
    return 0
  fi

  local parent_cmd expected
  parent_cmd="$(ps -p "$PPID" -o command= 2>/dev/null || true)"
  expected="$PYTHON_BIN -m feibot.gateway --config $CONFIG_FILE"
  [[ "$parent_cmd" == *"$expected"* ]]
}

launchd_command_is_expected() {
  local info
  if ! launchd_loaded; then
    return 1
  fi

  info="$(launchd_print)"
  [[ "$info" == *"exec \"$PYTHON_BIN\""* || "$info" == *"exec $PYTHON_BIN"* ]] \
    && [[ "$info" == *" -m feibot.gateway"* ]] \
    && [[ "$info" == *"--config \"$CONFIG_FILE\""* || "$info" == *"--config $CONFIG_FILE"* ]]
}

print_launchd_arguments_excerpt() {
  launchd_print | sed -n '/arguments = {/,/^[[:space:]]*}/p'
}

tail_log_if_present() {
  local n="${1:-80}"
  if [[ ! -f "$LOG_FILE" ]]; then
    echo "log_file: missing ($LOG_FILE)" >&2
    return 0
  fi
  tail -n "$n" "$LOG_FILE" || true
}

require_expected_launchd_command() {
  if launchd_command_is_expected; then
    return 0
  fi
  echo "ERROR: launchd ProgramArguments are unexpected for $LAUNCHD_LABEL." >&2
  print_launchd_arguments_excerpt >&2 || true
  echo "Run '$INSTALL_HINT' from a shell outside the service." >&2
  exit 1
}

cmd_install() {
  ensure_write_paths
  require_python_bin

  write_plist
  launchctl enable "$LAUNCHD_TARGET" >/dev/null 2>&1 || true
  unload_launchd
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
  ensure_write_paths
  require_python_bin

  local pid
  pid="$(running_pid)"
  if [[ -n "$pid" ]] && launchd_loaded; then
    write_pid_file
    echo "Already running: pid=$pid launchd=$LAUNCHD_LABEL"
    return 0
  fi

  if launchd_loaded; then
    require_expected_launchd_command
    launchctl kickstart "$LAUNCHD_TARGET"
  else
    write_plist
    launchctl enable "$LAUNCHD_TARGET" >/dev/null 2>&1 || true
    bootstrap_launchd
  fi

  sleep 2
  write_pid_file
  pid="$(running_pid)"
  if [[ -z "$pid" ]]; then
    echo "ERROR: start failed; process not found after launch." >&2
    tail_log_if_present 80
    exit 1
  fi

  echo "Started: pid=$pid launchd=$LAUNCHD_LABEL"
}

cmd_stop() {
  ensure_write_paths
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
  ensure_write_paths
  require_python_bin

  launchctl enable "$LAUNCHD_TARGET" >/dev/null 2>&1 || true

  if launchd_loaded; then
    if running_inside_target_job; then
      require_expected_launchd_command
      (launchctl kickstart -k "$LAUNCHD_TARGET" >/dev/null 2>&1) &
      disown "$!" >/dev/null 2>&1 || true
      echo "Restart requested: launchd=$LAUNCHD_LABEL (self)"
      return 0
    fi

    require_expected_launchd_command
    launchctl kickstart -k "$LAUNCHD_TARGET"
  else
    write_plist
    bootstrap_launchd
  fi

  sleep 2
  write_pid_file
  local pid
  pid="$(running_pid)"
  if [[ -z "$pid" ]]; then
    echo "ERROR: restart failed; process not found after launch." >&2
    tail_log_if_present 80
    exit 1
  fi

  echo "Restarted: pid=$pid launchd=$LAUNCHD_LABEL"
}

cmd_uninstall() {
  ensure_write_paths

  cmd_stop
  launchctl disable "$LAUNCHD_TARGET" >/dev/null 2>&1 || true
  rm -f "$PLIST_FILE"

  echo "Uninstalled: label=$LAUNCHD_LABEL plist_removed=$PLIST_FILE"
}

cmd_status() {
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
  tail_log_if_present 20
}

cmd_logs() {
  local n="${1:-120}"
  tail_log_if_present "$n"
}

discover_gateway_labels() {
  {
    if command -v launchctl >/dev/null 2>&1; then
      launchctl list 2>/dev/null | awk 'NF >= 3 && $3 ~ /^ai\..+\.gateway$/ { print $3 }' || true
    fi
    if [[ -d "$HOME/Library/LaunchAgents" ]]; then
      for plist in "$HOME"/Library/LaunchAgents/ai.*.gateway.plist; do
        [[ -e "$plist" ]] || continue
        basename "$plist" .plist
      done
    fi
  } | awk 'NF { print }' | sort -u
}

extract_config_from_plist() {
  local plist_file="$1"
  grep -Eo -- '--config "[^"]+"' "$plist_file" 2>/dev/null \
    | sed -E 's/^--config "([^"]+)"$/\1/' \
    | head -n1 || true
}

cmd_list() {
  local launch_rows labels label pid plist_file config_path
  local discovered_configs

  echo "=== Mybot Instances ==="
  echo ""

  if command -v launchctl >/dev/null 2>&1; then
    launch_rows="$(launchctl list 2>/dev/null | awk 'NF >= 3 && $3 ~ /^ai\..+\.gateway$/ { print $1 "\t" $3 }' || true)"
  else
    launch_rows=""
  fi
  labels="$(discover_gateway_labels)"

  echo "Launchd services:"
  if [[ -z "$labels" ]]; then
    echo "  (none found)"
  else
    while IFS= read -r label; do
      [[ -n "$label" ]] || continue
      pid="$(echo "$launch_rows" | awk -F'\t' -v label="$label" '$2 == label { print $1; exit }')"
      if [[ -z "$pid" ]]; then
        echo "  $label: installed (not loaded)"
      elif [[ "$pid" == "-" ]]; then
        echo "  $label: stopped"
      else
        echo "  $label: running (pid=$pid)"
      fi
    done <<< "$labels"
  fi

  discovered_configs=""
  if [[ -n "$labels" ]]; then
    while IFS= read -r label; do
      [[ -n "$label" ]] || continue
      plist_file="$HOME/Library/LaunchAgents/$label.plist"
      [[ -f "$plist_file" ]] || continue
      config_path="$(extract_config_from_plist "$plist_file")"
      [[ -n "$config_path" ]] || continue
      discovered_configs="${discovered_configs}${config_path}"$'\n'
    done <<< "$labels"
  fi

  if [[ -f "$CONFIG_FILE" ]]; then
    discovered_configs="${discovered_configs}${CONFIG_FILE}"$'\n'
  fi

  echo ""
  echo "Config files found:"
  if [[ -n "$discovered_configs" ]]; then
    echo "$discovered_configs" | awk 'NF { print }' | sort -u | while IFS= read -r config_path; do
      if [[ -f "$config_path" ]]; then
        echo "  $config_path"
      else
        echo "  $config_path (missing)"
      fi
    done
  else
    echo "  (none found)"
  fi
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
