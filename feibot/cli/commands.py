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
from feibot.channels.allow_from import extract_allow_from_open_ids
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
):
    """feibot - Personal AI Assistant."""
    return None


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
    """Create the appropriate LLM provider from config.

    Routing is driven by ``ProviderSpec.backend`` in the registry.
    """
    from feibot.providers.base import GenerationSettings
    from feibot.providers.openai_codex_provider import OpenAICodexProvider
    from feibot.providers.registry import find_by_name

    model = (config.agents.defaults.model or "").strip()
    if not model:
        console.print(f"[red]Error: Model is required in config ({config_path}).[/red]")
        raise typer.Exit(1)

    provider_name = config.get_provider_name(model)
    p = config.get_provider(model)
    spec = find_by_name(provider_name) if provider_name else None
    backend = spec.backend if spec else "openai_compat"

    # validation
    if backend == "openai_compat" and not model.startswith("bedrock/"):
        needs_key = not (p and p.api_key)
        exempt = spec and (spec.is_oauth or spec.is_local or spec.is_direct)
        if needs_key and not exempt:
            console.print("[red]Error: No API key configured.[/red]")
            console.print(f"Set one in {config_path} under providers section")
            raise typer.Exit(1)

    # instantiation by backend
    if backend == "openai_codex":
        provider = OpenAICodexProvider(default_model=model)
    elif backend == "anthropic":
        from feibot.providers.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(
            api_key=p.api_key if p else None,
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=p.extra_headers if p else None,
        )
    elif backend == "azure_openai":
        from feibot.providers.openai_compat_provider import OpenAICompatProvider

        provider = OpenAICompatProvider(
            api_key=p.api_key if p else None,
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=p.extra_headers if p else None,
            spec=spec,
        )
    else:
        from feibot.providers.openai_compat_provider import OpenAICompatProvider

        provider = OpenAICompatProvider(
            api_key=p.api_key if p else None,
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=p.extra_headers if p else None,
            spec=spec,
        )

    defaults = config.agents.defaults
    provider.generation = GenerationSettings(
        temperature=defaults.temperature,
        max_tokens=defaults.max_tokens,
        reasoning_effort=getattr(defaults, "reasoning_effort", None),
    )
    return provider


# ============================================================================
# Gateway / Server
# ============================================================================


def gateway(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    debug: bool = typer.Option(False, "--debug", "-d", help="Enable debug logging (record full LLM calls in session)"),
):
    """Start the feibot gateway."""
    from loguru import logger

    from feibot.agent.loop import AgentLoop
    from feibot.agent.tools.shell import ExecTool
    from feibot.bus.queue import MessageBus
    from feibot.channels.manager import ChannelManager
    from feibot.cron.service import CronService
    from feibot.cron.types import CronExecutionResult, CronJob, CronSchedule
    from feibot.heartbeat.service import HeartbeatService
    from feibot.history.service import HistorySyncService
    from feibot.session.manager import SessionManager

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    console.print(f"{__logo__} Starting feibot gateway on port {port}...")

    config_path, config, workspace, sessions = _load_runtime_config()
    bus = MessageBus()
    provider = _make_provider(config, config_path)
    session_manager = SessionManager(sessions)
    history_sync = HistorySyncService(
        workspace=workspace,
        session_manager=session_manager,
        provider=provider,
        model=config.agents.defaults.model,
    )
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
        max_consecutive_tool_errors=config.agents.defaults.max_consecutive_tool_errors,
        memory_window=config.agents.defaults.memory_window,
        brave_api_key=config.tools.web.search.api_key or None,
        skills_env=config.skills.env,
        skills_my_source=config.skills.sources.my,
        exec_config=config.tools.exec,
        feishu_config=config.channels.feishu,
        cron_service=cron,
        writable_dirs=config.tools.writable_dirs,
        allowed_hosts=config.tools.allowed_hosts,
        session_manager=session_manager,
        debug=debug,
        agent_name=config.name,
        disabled_tools=config.tools.disabled_tools,
        disable_all_tools=config.agents.defaults.disable_tools,
        include_skills=not config.agents.defaults.disable_skills,
        include_long_term_memory=not config.agents.defaults.disable_long_term_memory,
        madame_config=config.madame,
        config_path=config_path,
    )

    def _extract_structured_cron_result(response: str | None) -> dict | None:
        text = str(response or "").strip()
        if not text:
            return None
        try:
            candidate = json.loads(text)
            if isinstance(candidate, dict):
                return candidate
        except Exception:
            codeblock = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", text, flags=re.IGNORECASE)
            if codeblock:
                try:
                    candidate = json.loads(codeblock.group(1))
                    if isinstance(candidate, dict):
                        return candidate
                except Exception:
                    return None
        return None

    def _parse_cron_result(response: str | None) -> CronExecutionResult:
        """Parse structured cron output, with resilient text fallback."""
        text = str(response or "").strip()
        if not text:
            return CronExecutionResult(
                run_status="ok",
                business_status="no_change",
                delivery_status="not_requested",
                summary=None,
                user_message="",
            )

        parsed = _extract_structured_cron_result(text)

        if parsed is not None:
            run_status = str(parsed.get("run_status") or parsed.get("runStatus") or "ok").strip().lower()
            if run_status not in {"ok", "error", "skipped"}:
                run_status = "ok"
            business_status = str(
                parsed.get("business_status") or parsed.get("businessStatus") or "n_a"
            ).strip().lower()
            if business_status not in {"changed", "no_change", "error", "n_a"}:
                business_status = "n_a"
            summary = str(parsed.get("summary") or "").strip() or None
            user_message = str(
                parsed.get("user_message")
                or parsed.get("userMessage")
                or parsed.get("message")
                or ""
            ).strip()
            if not user_message:
                user_message = text
            fingerprint = str(parsed.get("fingerprint") or "").strip() or None
            error = str(parsed.get("error") or "").strip() or None
            return CronExecutionResult(
                run_status=run_status,
                business_status=business_status,
                delivery_status="not_requested",
                summary=summary,
                user_message=user_message,
                fingerprint=fingerprint,
                error=error,
            )

        lower_text = text.lower()
        no_change_markers = (
            "no new post",
            "no new posts",
            "no updates",
            "nothing new",
            "unchanged",
        )
        business_status = "no_change" if any(m in lower_text for m in no_change_markers) else "changed"
        summary = text.splitlines()[0].strip()[:280] if text else None
        return CronExecutionResult(
            run_status="ok",
            business_status=business_status,
            delivery_status="not_requested",
            summary=summary,
            user_message=text,
        )

    def _should_notify(job: CronJob, result: CronExecutionResult) -> bool:
        if result.run_status == "error":
            return bool(job.payload.notify_on_error)
        if job.payload.notify_policy == "always":
            return True
        if job.payload.notify_policy == "changes_only":
            return result.business_status == "changed"
        if job.payload.notify_policy == "digest":
            return result.business_status == "changed"
        return False

    async def _run_exec_cron_job(job: CronJob) -> CronExecutionResult:
        exec_tool = agent.tools.get("exec")
        if not isinstance(exec_tool, ExecTool):
            return CronExecutionResult(
                run_status="error",
                business_status="error",
                delivery_status="not_requested",
                error="exec tool is not available",
                summary=f"Cron job '{job.name}' failed",
            )

        command = str(job.payload.command or "").strip()
        if not command:
            return CronExecutionResult(
                run_status="error",
                business_status="error",
                delivery_status="not_requested",
                error="exec payload is missing command",
                summary=f"Cron job '{job.name}' failed",
            )

        output, returncode = await exec_tool.run_command(
            command,
            working_dir=job.payload.working_dir,
        )
        structured = _extract_structured_cron_result(output)
        result = _parse_cron_result(output)
        if returncode == 0 or structured is not None or result.run_status == "error":
            return result

        text = str(output or "").strip()
        detail = f"command exited with status {returncode}"
        if text:
            detail = f"{detail}: {text.splitlines()[0][:240]}"
        return CronExecutionResult(
            run_status="error",
            business_status="error",
            delivery_status="not_requested",
            error=detail,
            summary=f"Cron job '{job.name}' failed",
            user_message=text or f"Cron job '{job.name}' failed.",
        )

    async def on_cron_job(job: CronJob) -> CronExecutionResult:
        """Execute one cron job and return structured execution/delivery result."""
        try:
            if job.payload.kind == "system_event":
                if job.payload.message == "history_sync":
                    response = await history_sync.run()
                else:
                    response = f"Unknown system event: {job.payload.message}"
                result = _parse_cron_result(response)
            elif job.payload.kind == "exec":
                result = await _run_exec_cron_job(job)
            else:
                response = await agent.process_direct(
                    job.payload.message,
                    session_key=f"cron:{job.id}",
                    channel=job.payload.channel or "cli",
                    chat_id=job.payload.to or "direct",
                    metadata={"_suppress_progress": True},
                )
                result = _parse_cron_result(response)
        except Exception as e:
            result = CronExecutionResult(
                run_status="error",
                business_status="error",
                delivery_status="not_requested",
                error=str(e),
                summary=f"Cron job '{job.name}' failed",
            )

        notify = _should_notify(job, result)
        if result.run_status == "error" and not result.user_message:
            result.user_message = (
                f"Cron job '{job.name}' failed: {result.error or 'unknown error'}"
            )

        if not notify:
            result.delivery_status = "not_requested"
            return result

        deliver_channel = job.payload.channel
        deliver_to = job.payload.to
        if not deliver_to:
            fallback_channel, fallback_chat_id = _pick_heartbeat_target()
            if fallback_channel != "cli":
                deliver_channel = deliver_channel or fallback_channel
                deliver_to = fallback_chat_id

        content = str(result.user_message or result.summary or "").strip()
        if result.run_status == "error" and not content:
            content = f"Cron job '{job.name}' failed."

        if not deliver_to:
            result.delivery_status = "not_delivered"
            result.delivery_error = "No delivery target available"
            return result

        if not content:
            result.delivery_status = "not_requested"
            return result

        from feibot.bus.events import OutboundMessage

        try:
            await bus.publish_outbound(
                OutboundMessage(
                    channel=deliver_channel or "cli",
                    chat_id=deliver_to,
                    content=content,
                )
            )
            # Outbound dispatch is asynchronous; enqueue success means status is unknown.
            result.delivery_status = "unknown"
            result.delivery_error = None
        except Exception as e:
            result.delivery_status = "not_delivered"
            result.delivery_error = str(e)
        return result

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
        for path in logs_dir.rglob("*.jsonl"):
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
            for candidate in extract_allow_from_open_ids(
                list(getattr(fs_cfg, "allow_from", []) or [])
            ):
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

    heartbeat_channel, heartbeat_chat_id = _pick_heartbeat_target()

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print("[green]✓[/green] Heartbeat: every 30m")
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
        max_consecutive_tool_errors=config.agents.defaults.max_consecutive_tool_errors,
        memory_window=config.agents.defaults.memory_window,
        brave_api_key=config.tools.web.search.api_key or None,
        skills_env=config.skills.env,
        skills_my_source=config.skills.sources.my,
        exec_config=config.tools.exec,
        feishu_config=config.channels.feishu,
        cron_service=cron,
        writable_dirs=config.tools.writable_dirs,
        allowed_hosts=config.tools.allowed_hosts,
        session_manager=SessionManager(sessions),
        debug=debug,
        agent_name=config.name,
        disabled_tools=config.tools.disabled_tools,
        disable_all_tools=config.agents.defaults.disable_tools,
        include_skills=not config.agents.defaults.disable_skills,
        include_long_term_memory=not config.agents.defaults.disable_long_term_memory,
        madame_config=config.madame,
        config_path=config_path,
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
# Madame Bootstrap
# ============================================================================


madame_app = typer.Typer(help="Bootstrap Madame control plane")
app.add_typer(madame_app, name="madame")


def _parse_pool_slot(raw: str) -> tuple[str, str, str]:
    text = str(raw or "").strip()
    if "=" not in text:
        raise ValueError("pool slot must be '<display_name>=<app_id>:<app_secret>'")
    name, pair = text.split("=", 1)
    name = name.strip()
    if ":" not in pair:
        raise ValueError("pool slot credentials must be '<app_id>:<app_secret>'")
    app_id, app_secret = pair.split(":", 1)
    app_id = app_id.strip()
    app_secret = app_secret.strip()
    if not name or not app_id or not app_secret:
        raise ValueError("pool slot requires non-empty display_name, app_id, and app_secret")
    return name, app_id, app_secret


@madame_app.command("init")
def madame_init(
    repo_dir: Path = typer.Option(
        ...,
        "--repo-dir",
        help="Path to cloned feibot repository root",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
    ),
    madame_dir: Path = typer.Option(
        ...,
        "--madame-dir",
        help="Madame base directory to create",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
    ),
    app_id: str = typer.Option(..., "--app-id", help="Feishu app_id for Madame bot"),
    app_secret: str = typer.Option(..., "--app-secret", help="Feishu app_secret for Madame bot"),
    runtime_id: str = typer.Option("madame", "--runtime-id", help="Madame runtime id"),
    display_name: str = typer.Option("Madame", "--display-name", help="Madame display name"),
    model: str = typer.Option("openai/gpt-4o-mini", "--model", help="Default model"),
    pool_slot: list[str] = typer.Option(
        [],
        "--pool-slot",
        help="Named slot format: '<display_name>=<app_id>:<app_secret>'",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing config/registry"),
):
    """Initialize Madame runtime with local ops script and optional dynamic credential pool."""
    import stat

    from feibot.madame.registry import AgentRegistry, AgentRegistryStore, CredentialSlot, ManagedAgent

    repo_root = repo_dir.expanduser().resolve()
    madame_root = madame_dir.expanduser().resolve()
    workspace = madame_root / "workspace"
    sessions = madame_root / "sessions"
    run_dir = madame_root / "run"
    backup_dir = madame_root / "backups"
    shared_dir = madame_root / "shared"
    shared_workdir = shared_dir / "workdir"
    shared_skills_dir = shared_dir / "skills"
    shared_active_skills = shared_skills_dir / "active"
    shared_inactive_skills = shared_skills_dir / "inactive"
    ops_dir = madame_root / "ops"
    config_path = madame_root / "config.json"
    registry_path = madame_root / "agents_registry.json"
    base_dir_template = str(madame_root / "agents" / "{runtime_id}")

    script_source = repo_root / "feibot" / "skills" / "feibot-ops" / "scripts" / "manage.sh"
    if not script_source.exists():
        console.print(f"[red]Error: manage.sh not found at {script_source}[/red]")
        raise typer.Exit(1)

    if (config_path.exists() or registry_path.exists()) and not force:
        console.print(
            "[red]Error: target Madame config/registry already exists. Use --force to overwrite.[/red]"
        )
        raise typer.Exit(1)

    for path in [
        workspace,
        sessions,
        run_dir,
        backup_dir,
        ops_dir,
        shared_workdir,
        shared_active_skills,
        shared_inactive_skills,
    ]:
        path.mkdir(parents=True, exist_ok=True)
    (workspace / "skills").mkdir(parents=True, exist_ok=True)
    (workspace / "memory").mkdir(parents=True, exist_ok=True)

    ops_target = ops_dir / "manage.sh"
    repo_root_text = str(repo_root).replace("\\", "\\\\").replace('"', '\\"')
    script_source_text = str(script_source).replace("\\", "\\\\").replace('"', '\\"')
    config_path_text = str(config_path).replace("\\", "\\\\").replace('"', '\\"')
    run_dir_text = str(run_dir).replace("\\", "\\\\").replace('"', '\\"')
    launchd_label_text = f"ai.{runtime_id}.gateway".replace("\\", "\\\\").replace('"', '\\"')
    ops_target.write_text(
        (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n\n"
            f'export FEIBOT_REPO_DIR="${{FEIBOT_REPO_DIR:-{repo_root_text}}}"\n'
            f'export FEIBOT_CONFIG_FILE="${{FEIBOT_CONFIG_FILE:-{config_path_text}}}"\n'
            f'export FEIBOT_RUN_DIR="${{FEIBOT_RUN_DIR:-{run_dir_text}}}"\n'
            f'export FEIBOT_LAUNCHD_LABEL="${{FEIBOT_LAUNCHD_LABEL:-{launchd_label_text}}}"\n\n'
            f'exec "{script_source_text}" "$@"\n'
        ),
        encoding="utf-8",
    )
    ops_target.chmod(ops_target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    agents_md = workspace / "AGENTS.md"
    if not agents_md.exists() or force:
        agents_md.write_text(
            (
                f"# Agent Instructions\n\n"
                f"You are {display_name}. You are the Madame control-plane assistant.\n"
                "Handle lifecycle management and pool governance only.\n"
            ),
            encoding="utf-8",
        )
    memory_md = workspace / "memory" / "MEMORY.md"
    if not memory_md.exists() or force:
        memory_md.write_text("# Long-term Memory\n", encoding="utf-8")
    history_md = workspace / "memory" / "HISTORY.md"
    if not history_md.exists() or force:
        history_md.write_text("", encoding="utf-8")

    config_payload = {
        "name": runtime_id,
        "paths": {
            "workspace": str(workspace),
            "sessions": str(sessions),
        },
        "agents": {
            "defaults": {
                "model": model,
                "provider": "auto",
                "maxTokens": 8192,
                "temperature": 0.7,
                "maxToolIterations": 100,
                "maxConsecutiveToolErrors": 10,
                "memoryWindow": 50,
            }
        },
        "providers": {"openai": {"apiKey": ""}},
        "channels": {
            "feishu": {
                "enabled": True,
                "appId": app_id,
                "appSecret": app_secret,
                "allowFrom": [],
            }
        },
        "tools": {
            "writableDirs": [str(madame_root)],
            "allowedHosts": [],
            "exec": {},
        },
        "madame": {
            "enabled": True,
            "runtimeId": runtime_id,
            "registryPath": str(registry_path),
            "manageScript": str(ops_target),
            "baseDirTemplate": base_dir_template,
            "backupDir": str(backup_dir),
            "enforceIsolation": True,
        },
        "skills": {
            "env": {
                "FEIBOT_SHARED_WORKDIR": str(shared_workdir),
            }
        },
    }
    config_path.write_text(json.dumps(config_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    registry = AgentRegistry()
    registry.upsert_agent(
        ManagedAgent(
            runtime_id=runtime_id,
            display_name=display_name,
            mode="agent",
            role="manager",
            profile="manager",
            launchd_label=f"ai.{runtime_id}.gateway",
            config_path=str(config_path),
            workspace_path=str(workspace),
            sessions_path=str(sessions),
            run_dir=str(run_dir),
            app_id=app_id,
            app_secret=app_secret,
            tool_policy="madame_only",
            memory_mode="default",
            skill_mode="default",
        )
    )
    for raw in pool_slot:
        try:
            slot_name, slot_app_id, slot_app_secret = _parse_pool_slot(raw)
        except ValueError as exc:
            console.print(f"[red]Error parsing --pool-slot '{raw}': {exc}[/red]")
            raise typer.Exit(1) from exc
        registry.upsert_pool_slot(
            CredentialSlot(
                display_name=slot_name,
                app_id=slot_app_id,
                app_secret=slot_app_secret,
                status="available",
                assigned_runtime_id="",
            )
        )

    store = AgentRegistryStore(registry_path)
    store.save(registry)

    console.print(f"[green]✓[/green] Initialized Madame runtime at {madame_root}")
    console.print(f"[green]✓[/green] Config: {config_path}")
    console.print(f"[green]✓[/green] Registry: {registry_path}")
    console.print(f"[green]✓[/green] Ops wrapper written to: {ops_target}")
    console.print(f"[green]✓[/green] Dynamic pool slots: {len(registry.credential_pool)}")
    console.print(
        "[green]✓[/green] Bootstrap complete. Use in-chat `/agent ...` commands for management."
    )


if __name__ == "__main__":
    app()
