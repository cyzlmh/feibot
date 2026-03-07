"""CLI commands for feibot."""

import asyncio
import json
import os
import re
import select
import signal
import sys
from pathlib import Path

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from feibot import __logo__, __version__
from feibot.config.schema import Config
from feibot.utils.helpers import sync_workspace_templates

app = typer.Typer(
    name="feibot",
    help=f"{__logo__} feibot - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}
_CONFIG_PATH: Path | None = None

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None
_SAVED_TERM_ATTRS = None  # original termios settings, restored on exit


def _flush_pending_tty_input() -> None:
    """Drop unread keypresses typed while the model was generating output."""
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
    except Exception:
        return

    try:
        import termios
        termios.tcflush(fd, termios.TCIFLUSH)
        return
    except Exception:
        pass

    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            if not os.read(fd, 4096):
                break
    except Exception:
        return


def _restore_terminal() -> None:
    """Restore terminal to its original state (echo, line buffering, etc.)."""
    if _SAVED_TERM_ATTRS is None:
        return
    try:
        import termios
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)
    except Exception:
        pass


def _init_prompt_session(sessions_dir: Path) -> None:
    """Create the prompt_toolkit session with persistent file history."""
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # Save terminal state so we can restore it on exit
    try:
        import termios
        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    history_file = sessions_dir / "cli_history"
    history_file.parent.mkdir(parents=True, exist_ok=True)

    _PROMPT_SESSION = PromptSession(
        history=FileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,   # Enter submits (single line mode)
    )


def _print_agent_response(response: str, render_markdown: bool) -> None:
    """Render assistant response with consistent terminal styling."""
    content = response or ""
    body = Markdown(content) if render_markdown else Text(content)
    console.print()
    console.print(f"[cyan]{__logo__} feibot[/cyan]")
    console.print(body)
    console.print()


def _is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat."""
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """Read user input using prompt_toolkit (handles paste, history, display).

    prompt_toolkit natively handles:
    - Multiline paste (bracketed paste mode)
    - History navigation (up/down arrows)
    - Clean display (no ghost characters or artifacts)
    """
    if _PROMPT_SESSION is None:
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        with patch_stdout():
            return await _PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>You:</b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc



def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} feibot v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
    config: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to config file",
        exists=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
):
    """feibot - Personal AI Assistant."""
    global _CONFIG_PATH
    _CONFIG_PATH = config


def _get_config_path() -> Path:
    if _CONFIG_PATH is None:
        console.print("[red]Error: --config is required.[/red]")
        raise typer.Exit(2)
    return _CONFIG_PATH


def _load_runtime_config() -> tuple[Path, Config, Path, Path]:
    from feibot.config.loader import load_config

    config_path = _get_config_path()
    try:
        config = load_config(config_path)
    except Exception as exc:
        console.print(f"[red]Error: Invalid config at {config_path}: {exc}[/red]")
        raise typer.Exit(1) from exc

    workspace = config.resolve_workspace_path(config_path)
    sessions = config.resolve_sessions_path(config_path)

    created_workspace = False
    if not workspace.exists():
        workspace.mkdir(parents=True, exist_ok=True)
        created_workspace = True

    if not sessions.exists():
        sessions.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created sessions at {sessions}")

    sync_workspace_templates(workspace)
    if created_workspace:
        console.print(f"[green]✓[/green] Created workspace at {workspace}")

    return config_path, config, workspace, sessions


def _make_provider(config, config_path: Path):
    """Create LiteLLMProvider from config. Exits if no API key found."""
    from feibot.providers.litellm_provider import LiteLLMProvider
    p = config.get_provider()
    model = (config.agents.defaults.model or "").strip()
    if not model:
        console.print(f"[red]Error: Model is required in config ({config_path}).[/red]")
        raise typer.Exit(1)
    if not (p and p.api_key) and not model.startswith("bedrock/"):
        console.print("[red]Error: No API key configured.[/red]")
        console.print(f"Set one in {config_path} under providers section")
        raise typer.Exit(1)
    return LiteLLMProvider(
        api_key=p.api_key if p else None,
        api_base=config.get_api_base(),
        default_model=model,
        fallback_model=config.agents.defaults.fallback_model,
        llm_policy=config.agents.defaults.llm_policy,
        extra_headers=p.extra_headers if p else None,
        provider_name=config.get_provider_name(),
    )


# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def gateway(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    debug: bool = typer.Option(False, "--debug", "-d", help="Enable debug logging (record full LLM calls in session)"),
):
    """Start the feibot gateway."""
    from loguru import logger

    from feibot.agent.loop import AgentLoop
    from feibot.bus.queue import MessageBus
    from feibot.channels.manager import ChannelManager
    from feibot.cron.service import CronService
    from feibot.cron.types import CronJob
    from feibot.heartbeat.service import HeartbeatService
    from feibot.session.manager import SessionManager

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    console.print(f"{__logo__} Starting feibot gateway on port {port}...")

    config_path, config, workspace, sessions = _load_runtime_config()
    bus = MessageBus()
    provider = _make_provider(config, config_path)
    session_manager = SessionManager(sessions)
    cron_store_path = workspace / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        model=config.agents.defaults.model,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=config.agents.defaults.memory_window,
        brave_api_key=config.tools.web.search.api_key or None,
        exec_config=config.tools.exec,
        feishu_config=config.channels.feishu,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        allowed_dirs=config.tools.allowed_dirs,
        session_manager=session_manager,
        debug=debug,
    )

    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        response = await agent.process_direct(
            job.payload.message,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
            metadata={"_suppress_progress": True},
        )
        if job.payload.deliver and job.payload.to and response and response.strip():
            from feibot.bus.events import OutboundMessage

            await bus.publish_outbound(
                OutboundMessage(
                    channel=job.payload.channel or "cli",
                    chat_id=job.payload.to,
                    content=response or "",
                )
            )
        return response

    cron.on_job = on_cron_job

    # Create channel manager
    channels = ChannelManager(config, bus, workspace_dir=workspace)

    def _find_notify_group_marker() -> str | None:
        """
        Find a Feishu group chat marked by a user message containing only 'notify'.

        Uses raw channel logs because they preserve channel/chat metadata and survive
        session history trimming.
        """
        logs_dir = workspace / "logs"
        if not logs_dir.exists():
            return None

        latest_ts = ""
        latest_chat_id: str | None = None
        for path in logs_dir.glob("feishu_oc_*.jsonl"):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if rec.get("role") != "user":
                            continue
                        content = str(rec.get("content") or "").strip().lower()
                        # Feishu group mentions may be parsed as "@_user_1 notify".
                        tokens = [t for t in re.split(r"\s+", content) if t and not t.startswith("@")]
                        if tokens != ["notify"]:
                            continue
                        chat_id = str(rec.get("chat_id") or "")
                        if not chat_id.startswith("oc_"):
                            continue
                        ts = str(rec.get("timestamp") or "")
                        if ts >= latest_ts:
                            latest_ts = ts
                            latest_chat_id = chat_id
            except Exception:
                continue

        return latest_chat_id

    def _pick_heartbeat_target() -> tuple[str, str]:
        """
        Pick a deterministic route for heartbeat-triggered notifications.

        Priority:
        1. Config override: gateway.heartbeatTarget (channel:chat_id)
        2. Feishu group explicitly marked by sending 'notify'
        3. Feishu first allowlist entry (single-user fallback)
        4. CLI fallback (only if no Feishu target can be inferred)
        """
        raw_target = str(getattr(config.gateway, "heartbeat_target", "") or "").strip()
        if raw_target:
            if ":" in raw_target:
                channel, chat_id = raw_target.split(":", 1)
                channel = channel.strip()
                chat_id = chat_id.strip()
                if channel and chat_id:
                    return channel, chat_id
            logger.warning(
                "Invalid gateway.heartbeatTarget '{}', expected 'channel:chat_id'",
                raw_target,
            )

        if notify_group := _find_notify_group_marker():
            return "feishu", notify_group

        fs_cfg = config.channels.feishu
        if fs_cfg.enabled:
            for candidate in getattr(fs_cfg, "allow_from", []) or []:
                chat_id = str(candidate).strip()
                if chat_id:
                    return "feishu", chat_id

        return "cli", "direct"

    # Create heartbeat service
    async def on_heartbeat_execute(tasks: str) -> str:
        """Phase 2: execute heartbeat tasks through the full agent loop."""
        channel, chat_id = _pick_heartbeat_target()
        task_text = tasks.strip() or "Read HEARTBEAT.md in your workspace and execute active tasks."
        return await agent.process_direct(
            task_text,
            session_key="heartbeat",
            channel=channel,
            chat_id=chat_id,
            metadata={"_suppress_progress": True},
        )

    async def on_heartbeat_notify(response: str) -> None:
        """Deliver heartbeat execution result to the selected channel."""
        from feibot.bus.events import OutboundMessage

        channel, chat_id = _pick_heartbeat_target()
        if channel == "cli":
            return
        await bus.publish_outbound(OutboundMessage(channel=channel, chat_id=chat_id, content=response))

    heartbeat = HeartbeatService(
        workspace=workspace,
        provider=provider,
        model=agent.model,
        on_execute=on_heartbeat_execute,
        on_notify=on_heartbeat_notify,
        interval_s=30 * 60,  # 30 minutes
        enabled=True,
    )

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print("[green]✓[/green] Heartbeat: every 30m")
    heartbeat_channel, heartbeat_chat_id = _pick_heartbeat_target()
    console.print(
        f"[green]✓[/green] Heartbeat target: {heartbeat_channel}:{heartbeat_chat_id}"
    )

    async def run():
        try:
            await cron.start()
            await heartbeat.start()
            await asyncio.gather(
                agent.run(),
                channels.start_all(),
            )
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        finally:
            cron.stop()
            heartbeat.stop()
            agent.stop()
            await channels.stop_all()

    asyncio.run(run())




# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="Session ID"),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="Render assistant output as Markdown"),
    logs: bool = typer.Option(False, "--logs/--no-logs", help="Show feibot runtime logs during chat"),
    debug: bool = typer.Option(False, "--debug", "-d", help="Enable debug logging (record full LLM calls in session)"),
):
    """Interact with the agent directly."""
    from loguru import logger

    from feibot.agent.loop import AgentLoop
    from feibot.bus.queue import MessageBus
    from feibot.cron.service import CronService
    from feibot.session.manager import SessionManager

    config_path, config, workspace, sessions = _load_runtime_config()

    bus = MessageBus()
    provider = _make_provider(config, config_path)
    cron_store_path = workspace / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    if logs:
        logger.enable("feibot")
    else:
        logger.disable("feibot")

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        model=config.agents.defaults.model,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=config.agents.defaults.memory_window,
        brave_api_key=config.tools.web.search.api_key or None,
        exec_config=config.tools.exec,
        feishu_config=config.channels.feishu,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        allowed_dirs=config.tools.allowed_dirs,
        session_manager=SessionManager(sessions),
        debug=debug,
    )

    # Show spinner when logs are off (no output to miss); skip when logs are on
    def _thinking_ctx():
        if logs:
            from contextlib import nullcontext
            return nullcontext()
        # Animated spinner is safe to use with prompt_toolkit input handling
        return console.status("[dim]feibot is thinking...[/dim]", spinner="dots")

    if message:
        # Single message mode
        async def run_once():
            with _thinking_ctx():
                response = await agent_loop.process_direct(message, session_id)
            _print_agent_response(response, render_markdown=markdown)

        asyncio.run(run_once())
    else:
        # Interactive mode
        _init_prompt_session(sessions)
        console.print(f"{__logo__} Interactive mode (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit)\n")

        def _exit_on_sigint(signum, frame):
            _restore_terminal()
            console.print("\nGoodbye!")
            os._exit(0)

        signal.signal(signal.SIGINT, _exit_on_sigint)

        async def run_interactive():
            while True:
                try:
                    _flush_pending_tty_input()
                    user_input = await _read_interactive_input_async()
                    command = user_input.strip()
                    if not command:
                        continue

                    if _is_exit_command(command):
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break

                    with _thinking_ctx():
                        response = await agent_loop.process_direct(user_input, session_id)
                    _print_agent_response(response, render_markdown=markdown)
                except KeyboardInterrupt:
                    _restore_terminal()
                    console.print("\nGoodbye!")
                    break
                except EOFError:
                    _restore_terminal()
                    console.print("\nGoodbye!")
                    break

        asyncio.run(run_interactive())


# ============================================================================
# Cron Commands
# ============================================================================


cron_app = typer.Typer(help="Manage scheduled tasks")
app.add_typer(cron_app, name="cron")


@cron_app.command("list")
def cron_list(
    all: bool = typer.Option(False, "--all", "-a", help="Include disabled jobs"),
):
    """List scheduled jobs."""
    import time
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    from feibot.cron.service import CronService

    _, _, workspace, _ = _load_runtime_config()
    store_path = workspace / "cron" / "jobs.json"
    service = CronService(store_path)

    jobs = service.list_jobs(include_disabled=all)

    if not jobs:
        console.print("No scheduled jobs.")
        return

    table = Table(title="Scheduled Jobs")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Schedule")
    table.add_column("Status")
    table.add_column("Next Run")

    for job in jobs:
        if job.schedule.kind == "every":
            sched = f"every {(job.schedule.every_ms or 0) // 1000}s"
        elif job.schedule.kind == "cron":
            sched = (
                f"{job.schedule.expr or ''} ({job.schedule.tz})"
                if job.schedule.tz
                else (job.schedule.expr or "")
            )
        else:
            sched = "one-time"

        next_run = ""
        if job.state.next_run_at_ms:
            ts = job.state.next_run_at_ms / 1000
            try:
                tz = ZoneInfo(job.schedule.tz) if job.schedule.tz else None
                next_run = _dt.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M")
            except Exception:
                next_run = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))

        status = "[green]enabled[/green]" if job.enabled else "[dim]disabled[/dim]"
        table.add_row(job.id, job.name, sched, status, next_run)

    console.print(table)


@cron_app.command("add")
def cron_add(
    name: str = typer.Option(..., "--name", "-n", help="Job name"),
    message: str = typer.Option(..., "--message", "-m", help="Message for agent"),
    every: int = typer.Option(None, "--every", "-e", help="Run every N seconds"),
    cron_expr: str = typer.Option(None, "--cron", "-c", help="Cron expression (e.g. '0 9 * * *')"),
    tz: str | None = typer.Option(None, "--tz", help="IANA timezone for cron (e.g. 'America/Vancouver')"),
    at: str = typer.Option(None, "--at", help="Run once at time (ISO format)"),
    deliver: bool = typer.Option(False, "--deliver", "-d", help="Deliver response to channel"),
    to: str = typer.Option(None, "--to", help="Recipient for delivery"),
    channel: str = typer.Option(None, "--channel", help="Channel for delivery (e.g. 'feishu')"),
):
    """Add a scheduled job."""
    import datetime

    from feibot.cron.service import CronService
    from feibot.cron.types import CronSchedule

    if tz and not cron_expr:
        console.print("[red]Error: --tz can only be used with --cron[/red]")
        raise typer.Exit(1)

    if channel and channel != "feishu":
        console.print("[red]Error: --channel only supports 'feishu'[/red]")
        raise typer.Exit(1)

    if deliver and not to:
        console.print("[red]Error: --to is required when --deliver is set[/red]")
        raise typer.Exit(1)

    if deliver and not channel:
        channel = "feishu"

    if every:
        schedule = CronSchedule(kind="every", every_ms=every * 1000)
    elif cron_expr:
        schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz)
    elif at:
        try:
            dt = datetime.datetime.fromisoformat(at)
        except ValueError as e:
            console.print(f"[red]Error: invalid --at datetime '{at}': {e}[/red]")
            raise typer.Exit(1) from e
        schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
    else:
        console.print("[red]Error: Must specify --every, --cron, or --at[/red]")
        raise typer.Exit(1)

    _, _, workspace, _ = _load_runtime_config()
    store_path = workspace / "cron" / "jobs.json"
    service = CronService(store_path)

    try:
        job, status = service.upsert_job(
            name=name,
            schedule=schedule,
            message=message,
            deliver=deliver,
            to=to,
            channel=channel,
            delete_after_run=(schedule.kind == "at"),
        )
    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from e

    if status == "created":
        console.print(f"[green]✓[/green] Added job '{job.name}' ({job.id})")
    elif status == "updated":
        console.print(f"[yellow]✓[/yellow] Updated job '{job.name}' ({job.id})")
    else:
        console.print(f"[cyan]✓[/cyan] Job unchanged '{job.name}' ({job.id})")


@cron_app.command("remove")
def cron_remove(
    job_id: str = typer.Argument(..., help="Job ID to remove"),
):
    """Remove a scheduled job."""
    from feibot.cron.service import CronService

    _, _, workspace, _ = _load_runtime_config()
    store_path = workspace / "cron" / "jobs.json"
    service = CronService(store_path)

    if service.remove_job(job_id):
        console.print(f"[green]✓[/green] Removed job {job_id}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("enable")
def cron_enable(
    job_id: str = typer.Argument(..., help="Job ID"),
    disable: bool = typer.Option(False, "--disable", help="Disable instead of enable"),
):
    """Enable or disable a job."""
    from feibot.cron.service import CronService

    _, _, workspace, _ = _load_runtime_config()
    store_path = workspace / "cron" / "jobs.json"
    service = CronService(store_path)

    job = service.enable_job(job_id, enabled=not disable)
    if job:
        status = "disabled" if disable else "enabled"
        console.print(f"[green]✓[/green] Job '{job.name}' {status}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("run")
def cron_run(
    job_id: str = typer.Argument(..., help="Job ID to run"),
    force: bool = typer.Option(False, "--force", "-f", help="Run even if disabled"),
):
    """Manually run a job."""
    from loguru import logger

    from feibot.agent.loop import AgentLoop
    from feibot.bus.queue import MessageBus
    from feibot.cron.service import CronService
    from feibot.cron.types import CronJob
    from feibot.session.manager import SessionManager

    logger.disable("feibot")

    config_path, config, workspace, sessions = _load_runtime_config()
    provider = _make_provider(config, config_path)
    bus = MessageBus()

    store_path = workspace / "cron" / "jobs.json"
    service = CronService(store_path)

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        model=config.agents.defaults.model,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=config.agents.defaults.memory_window,
        brave_api_key=config.tools.web.search.api_key or None,
        exec_config=config.tools.exec,
        feishu_config=config.channels.feishu,
        cron_service=service,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        allowed_dirs=config.tools.allowed_dirs,
        session_manager=SessionManager(sessions),
    )

    result_holder: list[str | None] = []

    async def on_job(job: CronJob) -> str | None:
        response = await agent_loop.process_direct(
            job.payload.message,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
            metadata={"_suppress_progress": True},
        )
        result_holder.append(response)
        return response

    service.on_job = on_job

    async def run_job() -> bool:
        return await service.run_job(job_id, force=force)

    if asyncio.run(run_job()):
        console.print("[green]✓[/green] Job executed")
        if result_holder and result_holder[0]:
            _print_agent_response(result_holder[0], render_markdown=True)
    else:
        console.print(f"[red]Failed to run job {job_id}[/red]")


if __name__ == "__main__":
    app()
