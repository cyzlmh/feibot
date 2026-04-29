"""Madame command controller for agent lifecycle and capability orchestration."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from feibot.madame.registry import AgentRegistryStore, CredentialSlot, ManagedAgent

KNOWN_TOOL_NAMES = [
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "find_file",
    "grep_text",
    "exec",
    "web_search",
    "web_fetch",
    "cron",
    "message",
    "feishu_send_file",
]
CHAT_ALLOWED_TOOLS = {"web_search", "web_fetch"}


class AgentMadameController:
    """Parse and execute `/agent ...` Madame commands."""

    def __init__(
        self,
        *,
        workspace: Path,
        repo_dir: Path,
        registry_path: Path,
        madame_runtime_id: str = "madame",
        manage_script: Path | None = None,
        base_dir_template: str = "",
        backup_dir: Path | None = None,
        my_skills_source: str = "",
    ):
        self.workspace = workspace.expanduser().resolve()
        self.repo_dir = repo_dir.expanduser().resolve()
        self.store = AgentRegistryStore(registry_path)
        self.madame_runtime_id = str(madame_runtime_id or "madame").strip() or "madame"
        self.manage_script = self._resolve_manage_script(manage_script)
        self.base_dir_template = str(base_dir_template or "").strip()
        if backup_dir is None:
            self.backup_dir = (self.workspace / "backups").resolve()
        else:
            self.backup_dir = Path(backup_dir).expanduser().resolve()
        self.my_skills_source = str(my_skills_source or "").strip()
        self._runtime_loop: asyncio.AbstractEventLoop | None = None
        self._runtime_cron_service = None

    def bind_runtime(
        self,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        cron_service=None,
    ) -> None:
        """Bind runtime handles for commands that need live gateway services."""
        self._runtime_loop = loop
        self._runtime_cron_service = cron_service

    def execute(self, command_args: str) -> str:
        tokens = self._split(command_args)
        if not tokens:
            return self._help_text()

        command = tokens[0].lower()
        rest = tokens[1:]
        try:
            if command == "help":
                return self._help_text()
            if command == "list":
                return self._list_agents()
            if command == "status":
                return self._status_agent(rest)
            if command == "create":
                return self._create_agent(rest)
            if command == "start":
                return self._lifecycle_command(rest, "start")
            if command == "stop":
                return self._lifecycle_command(rest, "stop")
            if command == "restart":
                if rest and str(rest[0] or "").strip().lower() == "all":
                    return self._restart_all_agents()
                return self._lifecycle_command(rest, "restart")
            if command == "archive":
                return self._archive_agent(rest)
            if command == "skills":
                return self._skills_command(rest)
            if command == "cron":
                return self._cron_command(rest)
            if command == "pool":
                return self._pool_command(rest)
            return f"Unknown /agent command: {command}\n\n{self._help_text()}"
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error executing /agent {command}: {e}"

    def _resolve_manage_script(self, manage_script: Path | None) -> Path | None:
        if manage_script is None:
            return None
        path = Path(manage_script).expanduser().resolve()
        if not path.exists() or not path.is_file():
            return None
        return path

    @staticmethod
    def _split(command_args: str) -> list[str]:
        text = str(command_args or "").strip()
        if not text:
            return []
        return shlex.split(text)

    @staticmethod
    def _mask_secret(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return "(empty)"
        if len(text) <= 6:
            return "*" * len(text)
        return f"{text[:3]}***{text[-2:]}"

    @staticmethod
    def _md_cell(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return "-"
        return text.replace("|", r"\|").replace("\n", "<br>")

    @staticmethod
    def _normalize_mode(value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized == "pure_chat":
            return "chat"
        return normalized

    def _help_text(self) -> str:
        return (
            "Madame commands:\n"
            "/agent list\n"
            "/agent status <runtime_id>\n"
            "/agent create --name <id> --mode <agent|chat> "
            "[--model <model>] [--base-dir <path>] [--allow-from <id1,id2,...>] [--skill <skill1,skill2,...>]\n"
            "/agent restart all\n"
            "/agent start|stop|restart <runtime_id>\n"
            "/agent archive <runtime_id>\n"
            "/agent pool list\n"
            "/agent pool add --name <name> --app-id <id> --app-secret <secret>\n"
            "/agent pool remove <name>\n"
            "/agent skills <hub|agent> ...\n"
            "/agent cron <list|add|runs|remove|enable|disable|run> ...\n"
            "\n"
            "Shortcuts:\n"
            "/skillhub ... (same as /agent skills hub)\n"
            "/skill ... (same as /agent skills agent)"
        )

    def execute_skills(self, command_args: str) -> str:
        """Execute /skills commands (shortcut for /agent skills)."""
        tokens = self._split(command_args)
        if not tokens:
            return self._skills_help_text()
        try:
            return self._skills_command(tokens)
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error executing /skills: {e}"

    def _list_agents(self) -> str:
        registry = self.store.load()
        lines = [
            "Madame managed agents:",
            "",
            "| Runtime ID | Mode | Status | Slot | Service |",
            "| --- | --- | --- | --- | --- |",
        ]
        agents = sorted(registry.agents, key=lambda x: x.runtime_id)
        if not agents:
            lines.append("| (none) | - | - | - | - |")
        for agent in agents:
            service_state = self._launchd_state(agent.launchd_label)
            lifecycle = "archived" if agent.archived else "active"
            lines.append(
                "| "
                f"{self._md_cell(agent.runtime_id)} | "
                f"{self._md_cell(agent.mode)} | "
                f"{self._md_cell(lifecycle)} | "
                f"{self._md_cell(agent.slot_name)} | "
                f"{self._md_cell(service_state)} |"
            )

        available = sum(1 for slot in registry.credential_pool if slot.status == "available")
        assigned = sum(1 for slot in registry.credential_pool if slot.status == "assigned")
        lines.extend(
            [
                "",
                "Pool summary:",
                "",
                "| Total | Available | Assigned |",
                "| ---: | ---: | ---: |",
                f"| {len(registry.credential_pool)} | {available} | {assigned} |",
            ]
        )
        return "\n".join(lines)

    def _status_agent(self, args: list[str]) -> str:
        runtime_id = self._require_runtime_id(args)
        registry = self.store.load()
        agent = registry.get_agent(runtime_id)
        if agent is None:
            raise ValueError(f"Agent '{runtime_id}' not found in registry.")

        lines = [
            f"runtime_id: {agent.runtime_id}",
            f"mode: {agent.mode}",
            f"role: {agent.role}",
            f"profile: {agent.profile}",
            f"slot_name: {agent.slot_name or '(none)'}",
            f"archived: {agent.archived}",
            f"archived_at: {agent.archived_at or '(none)'}",
            f"backup_path: {agent.backup_path or '(none)'}",
            f"launchd_label: {agent.launchd_label}",
            f"service_state: {self._launchd_state(agent.launchd_label)}",
            f"config_path: {agent.config_path}",
            f"workspace_path: {agent.workspace_path}",
            f"sessions_path: {agent.sessions_path or '(none)'}",
            f"run_dir: {agent.run_dir}",
            f"app_id: {self._mask_secret(agent.app_id)}",
            f"app_secret: {self._mask_secret(agent.app_secret)}",
            f"skills: {', '.join(agent.skills) if agent.skills else '(none)'}",
            f"tool_policy: {agent.tool_policy}",
            f"memory_mode: {agent.memory_mode}",
            f"skill_mode: {agent.skill_mode}",
        ]
        return "\n".join(lines)

    def _create_agent(self, args: list[str]) -> str:
        if not args:
            raise ValueError("Usage: /agent create --name <id> --mode <agent|chat>")

        options = self._parse_options(args)
        runtime_id = self._normalize_id(options.get("name", "").strip())
        mode = self._normalize_mode(options.get("mode", ""))
        if not runtime_id or not mode:
            raise ValueError("create requires --name and --mode.")
        if mode not in {"agent", "chat"}:
            raise ValueError("mode must be either 'agent' or 'chat'.")

        registry = self.store.load()
        default_model = self._resolve_default_model(registry)
        model = options.get("model", "").strip() or default_model
        allow_from = [x.strip() for x in options.get("allow-from", "").split(",") if x.strip()]
        skills = self._csv_values(options.get_all("skill"))

        slot = registry.get_pool_slot(runtime_id)
        if slot is None:
            raise ValueError(
                f"No credential slot named '{runtime_id}'. Add it with /agent pool add first."
            )
        if slot.status != "available":
            owner = slot.assigned_runtime_id or "unknown"
            raise ValueError(
                f"Credential slot '{runtime_id}' is already assigned to runtime '{owner}'."
            )

        existing = registry.get_agent(runtime_id)
        if existing is not None:
            raise ValueError(f"Agent '{runtime_id}' already exists.")
        role = "chater" if mode == "chat" else "agent"
        profile = role

        base_dir = Path(options.get("base-dir", self._render_base_dir(runtime_id))).expanduser()
        config_path = Path(options.get("config-path", str(base_dir / "config.json"))).expanduser()
        workspace_path = Path(options.get("workspace-path", str(base_dir / "workspace"))).expanduser()
        sessions_path = Path(options.get("sessions-path", str(base_dir / "sessions"))).expanduser()
        run_dir = Path(options.get("run-dir", str(base_dir / "run"))).expanduser()
        launchd_label = options.get("launchd-label", f"ai.{runtime_id}.gateway").strip()

        role_defaults = self._role_defaults(role)
        provider_defaults = self._resolve_provider_defaults(registry)
        tool_defaults = self._resolve_tool_defaults(registry)
        agent = ManagedAgent(
            runtime_id=runtime_id,
            mode=mode,
            role=role,
            profile=profile,
            launchd_label=launchd_label,
            config_path=str(config_path.resolve()),
            workspace_path=str(workspace_path.resolve()),
            sessions_path=str(sessions_path.resolve()),
            run_dir=str(run_dir.resolve()),
            slot_name=runtime_id,
            app_id=slot.app_id,
            app_secret=slot.app_secret,
            archived=False,
            skills=skills,
            tool_policy=role_defaults["tool_policy"],
            memory_mode=role_defaults["memory_mode"],
            skill_mode=role_defaults["skill_mode"],
        )

        self._init_agent_files(
            agent=agent,
            workspace_path=workspace_path,
            sessions_path=sessions_path,
            run_dir=run_dir,
            config_path=config_path,
            model=model,
            allow_from=allow_from,
            providers=provider_defaults,
            tool_defaults=tool_defaults,
        )

        slot.status = "assigned"
        slot.assigned_runtime_id = runtime_id

        registry.upsert_agent(agent)
        registry.upsert_pool_slot(slot)
        self.store.save(registry)
        self._reconcile_agent_skills(agent)
        return (
            f"Created agent '{runtime_id}'.\n"
            f"- mode={mode} role={role}\n"
            f"- slot={slot.display_name}\n"
            f"- config={config_path.resolve()}\n"
            f"- workspace={workspace_path.resolve()}\n"
            f"- run_dir={run_dir.resolve()}"
        )

    def _lifecycle_command(self, args: list[str], action: str) -> str:
        runtime_id = self._require_runtime_id(args)
        registry = self.store.load()
        agent = registry.get_agent(runtime_id)
        if agent is None:
            raise ValueError(f"Agent '{runtime_id}' not found in registry.")
        if action in {"start", "restart"} and agent.archived:
            raise ValueError(f"Agent '{runtime_id}' is archived. Create a new agent to reactivate.")

        output = self._run_manage_script(agent, action)
        return (
            f"{action} -> {runtime_id}\n"
            f"service_state={self._launchd_state(agent.launchd_label)}\n"
            f"{output}"
        )

    def _restart_all_agents(self) -> str:
        registry = self.store.load()
        targets = [
            agent
            for agent in sorted(registry.agents, key=lambda x: x.runtime_id)
            if not agent.archived and agent.role != "manager"
        ]
        if not targets:
            return "No managed sub agents found."

        restarted: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []

        for agent in targets:
            service_state = self._launchd_state(agent.launchd_label)
            if service_state == "not_loaded":
                skipped.append(f"{agent.runtime_id} (not_loaded)")
                continue
            try:
                self._run_manage_script(agent, "restart")
            except Exception as exc:
                failed.append(f"{agent.runtime_id} ({exc})")
                continue
            restarted.append(f"{agent.runtime_id} ({self._launchd_state(agent.launchd_label)})")

        lines = [
            "restart all summary:",
            f"- restarted: {', '.join(restarted) if restarted else '(none)'}",
            f"- skipped: {', '.join(skipped) if skipped else '(none)'}",
            f"- failed: {', '.join(failed) if failed else '(none)'}",
        ]
        return "\n".join(lines)

    def _archive_agent(self, args: list[str]) -> str:
        runtime_id = self._require_runtime_id(args)
        registry = self.store.load()
        agent = registry.get_agent(runtime_id)
        if agent is None:
            raise ValueError(f"Agent '{runtime_id}' not found in registry.")

        out_uninstall = self._run_manage_script(agent, "uninstall")
        backup_path = self._backup_agent_files(agent)

        # Release credential slot if assigned.
        if agent.slot_name:
            slot = registry.get_pool_slot(agent.slot_name)
            if slot is not None:
                slot.status = "available"
                slot.assigned_runtime_id = ""
                registry.upsert_pool_slot(slot)

        # Remove agent from registry and clean up workdir.
        registry.remove_agent(runtime_id)
        self.store.save(registry)

        base_dir = Path(self._render_base_dir(runtime_id))
        if base_dir.exists():
            self._remove_path(base_dir)

        return (
            f"Archived {runtime_id}.\n"
            f"backup={backup_path}\n"
            f"service_state={self._launchd_state(agent.launchd_label)}\n"
            f"{out_uninstall}"
        )

    def _pool_command(self, args: list[str]) -> str:
        if not args:
            return (
                "Pool commands:\n"
                "/agent pool list\n"
                "/agent pool add --name <display_name> --app-id <id> --app-secret <secret>\n"
                "/agent pool remove <display_name>"
            )
        sub = str(args[0] or "").strip().lower()
        if sub == "list":
            return self._pool_list()
        if sub == "add":
            return self._pool_add(args[1:])
        if sub == "remove":
            return self._pool_remove(args[1:])
        raise ValueError(f"Unknown pool subcommand '{sub}'.")

    def _pool_list(self) -> str:
        registry = self.store.load()
        if not registry.credential_pool:
            return "Credential pool is empty."
        lines = ["Credential pool:"]
        for slot in sorted(registry.credential_pool, key=lambda x: x.display_name.lower()):
            owner = slot.assigned_runtime_id or "(none)"
            lines.append(
                f"- {slot.display_name} status={slot.status} owner={owner} app_id={self._mask_secret(slot.app_id)}"
            )
        return "\n".join(lines)

    def _pool_add(self, args: list[str]) -> str:
        options = self._parse_options(args)
        name = options.get("name", "").strip()
        app_id = options.get("app-id", "").strip()
        app_secret = options.get("app-secret", "").strip()
        if not name or not app_id or not app_secret:
            raise ValueError("pool add requires --name, --app-id, and --app-secret.")

        slot_id = self._normalize_id(name)
        registry = self.store.load()
        existing = registry.get_pool_slot(slot_id)
        if existing is not None and existing.status == "assigned":
            owner = existing.assigned_runtime_id or "unknown"
            raise ValueError(
                f"Cannot overwrite assigned slot '{slot_id}'. Currently used by runtime '{owner}'."
            )

        slot = CredentialSlot(
            display_name=slot_id,
            app_id=app_id,
            app_secret=app_secret,
            status="available",
            assigned_runtime_id="",
        )
        registry.upsert_pool_slot(slot)
        self.store.save(registry)
        return f"Upserted credential slot '{slot_id}'. total_slots={len(registry.credential_pool)}"

    def _pool_remove(self, args: list[str]) -> str:
        name = str(args[0] if args else "").strip()
        if not name:
            raise ValueError("Usage: /agent pool remove <name>")

        registry = self.store.load()
        slot = registry.get_pool_slot(name)
        if slot is None:
            raise ValueError(f"Credential slot '{name}' not found.")
        if slot.status == "assigned":
            owner = slot.assigned_runtime_id or "unknown"
            raise ValueError(f"Credential slot '{name}' is assigned to '{owner}', cannot remove.")

        registry.remove_pool_slot(name)
        self.store.save(registry)
        return f"Removed credential slot '{name}'. total_slots={len(registry.credential_pool)}"

    def _skills_command(self, args: list[str]) -> str:
        if not args:
            return self._skills_help_text()
        sub = str(args[0] or "").strip().lower()
        rest = args[1:]
        if sub == "hub":
            return self._skills_hub_command(rest)
        if sub == "agent":
            return self._skills_agent_command(rest)
        return f"Unknown /skills group: '{sub}'\n\n{self._skills_help_text()}"

    @staticmethod
    def _skills_help_text() -> str:
        return (
            "Skills commands:\n\n"
            "Hub (shared library):\n"
            "/skillhub list\n"
            "/skillhub find [query]\n"
            "/skillhub install <pkg>  (prefix 'my/' for personal repo)\n"
            "/skillhub uninstall <name>\n\n"
            "Agent assignment:\n"
            "/skill list\n"
            "/skill show <id>\n"
            "/skill add <id> <skill1,skill2,...>\n"
            "/skill remove <id> <skill1,skill2,...>\n"
            "/skill sync <id>\n"
            "/skill clear <id>"
        )

    # ---- Hub subcommands ----

    def _skills_hub_command(self, args: list[str]) -> str:
        if not args:
            return (
                "Hub commands:\n"
                "/skillhub list\n"
                "/skillhub find [query]\n"
                "/skillhub install <pkg>  (prefix 'my/' for personal repo)\n"
                "/skillhub uninstall <name>"
            )
        sub = str(args[0] or "").strip().lower()
        rest = args[1:]
        if sub == "list":
            return self._skills_hub_list(rest)
        if sub == "find":
            return self._skills_hub_find(rest)
        if sub == "install":
            return self._skills_hub_install(rest)
        if sub == "uninstall":
            return self._skills_hub_uninstall(rest)
        raise ValueError(f"Unknown hub subcommand: '{sub}'")

    def _skills_hub_list(self, args: list[str]) -> str:
        if args:
            raise ValueError("Usage: /skillhub list")
        state = self._refresh_shared_skill_state()
        installed = list(state["installed"])
        if not installed:
            return f"No skills installed in hub ({self._shared_install_root()})"
        lines = [f"Hub skills ({self._shared_install_root()}):"]
        for name in installed:
            lines.append(f"- {name}")
        return "\n".join(lines)

    def _skills_hub_find(self, args: list[str]) -> str:
        npx = shutil.which("npx")
        if not npx:
            raise ValueError("npx not found in PATH.")
        proc = subprocess.run(
            [npx, "skills", "find"] + args,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = self._strip_ansi((proc.stdout or "") + (proc.stderr or "")).strip()
        return output or "No results."

    def _skills_hub_install(self, args: list[str]) -> str:
        if not args:
            raise ValueError("Usage: /skillhub install <pkg>  (prefix 'my/' for personal repo)")
        npx = shutil.which("npx")
        if not npx:
            raise ValueError("npx not found in PATH.")

        pkg = args[0]
        extra_args = args[1:]

        if pkg.startswith("my/"):
            skill_name = pkg[3:]
            if not self.my_skills_source:
                raise ValueError(
                    "Personal skills source not configured. Set skills.sources.my in config."
                )
            source_path = Path(self.my_skills_source).expanduser().resolve()
            if not source_path.exists():
                raise ValueError(f"Personal skills source not found: {source_path}")
            pkg_spec = f"{source_path}@{skill_name}" if skill_name else str(source_path)
        else:
            pkg_spec = pkg

        install_dir = self._shared_root()
        install_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [npx, "skills", "add", "-y", pkg_spec] + extra_args,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(install_dir),
        )
        output = self._strip_ansi((proc.stdout or "") + (proc.stderr or "")).strip()
        if proc.returncode != 0:
            raise ValueError(f"skills install failed:\n{output}")
        return output or "Install complete."

    def _skills_hub_uninstall(self, args: list[str]) -> str:
        if not args:
            raise ValueError("Usage: /skillhub uninstall <name>")
        name = args[0]
        target = self._shared_install_root() / name
        if not target.exists():
            raise ValueError(f"Skill '{name}' not found in hub.")
        shutil.rmtree(target)
        registry = self.store.load()
        touched = self._reconcile_all_agent_skills(registry)
        lines = [f"Skill '{name}' removed from hub."]
        if touched:
            lines.append(f"Reconciled agents: {', '.join(touched)}")
        return "\n".join(lines)

    @staticmethod
    def _strip_ansi(text: str) -> str:
        text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
        text = re.sub(r"\x1b\][^\x07]*\x07", "", text)
        text = re.sub(r"[\r\x00]", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text

    # ---- Agent subcommands ----

    def _skills_agent_command(self, args: list[str]) -> str:
        if not args:
            return (
                "Agent skill commands:\n"
                "/skill list\n"
                "/skill show <id>\n"
                "/skill add <id> <skill1,skill2,...>\n"
                "/skill remove <id> <skill1,skill2,...>\n"
                "/skill sync <id>\n"
                "/skill clear <id>"
            )
        sub = str(args[0] or "").strip().lower()
        rest = args[1:]
        if sub == "list":
            return self._skills_agent_list(rest)
        if sub == "show":
            return self._skills_agent_show(rest)
        if sub == "add":
            return self._skills_agent_add(rest)
        if sub == "remove":
            return self._skills_agent_remove(rest)
        if sub == "sync":
            return self._skills_agent_sync(rest)
        if sub == "clear":
            return self._skills_agent_clear(rest)
        raise ValueError(f"Unknown agent subcommand: '{sub}'")

    def _skills_agent_list(self, args: list[str]) -> str:
        if args:
            raise ValueError("Usage: /skill list")
        registry = self.store.load()
        if not registry.agents:
            return "No agents in registry."
        lines = ["agents and their skills:"]
        for agent in registry.agents:
            skills_str = ", ".join(agent.skills) if agent.skills else "(none)"
            suffix = " [archived]" if agent.archived else ""
            lines.append(f"- {agent.runtime_id}{suffix}: {skills_str}")
        return "\n".join(lines)

    def _skills_agent_show(self, args: list[str]) -> str:
        runtime_id = self._require_runtime_id(args)
        registry = self.store.load()
        agent = registry.get_agent(runtime_id)
        if agent is None:
            raise ValueError(f"Agent '{runtime_id}' not found in registry.")
        state = self._refresh_shared_skill_state()
        assigned = list(agent.skills)
        installed = set(state["installed"])
        linked, local_only, conflicts = self._inspect_agent_skills(agent)
        missing = [name for name in assigned if name not in installed]
        lines = [
            f"skills for {runtime_id}:",
            f"- assigned: {', '.join(assigned) if assigned else '(none)'}",
            f"- linked: {', '.join(linked) if linked else '(none)'}",
            f"- missing: {', '.join(missing) if missing else '(none)'}",
            f"- conflicts: {', '.join(conflicts) if conflicts else '(none)'}",
            f"- local_workspace_skills: {', '.join(local_only) if local_only else '(none)'}",
        ]
        if agent.mode == "chat":
            lines.append("- note: chat agents do not materialize shared skills")
        elif agent.archived:
            lines.append("- note: archived agents do not materialize shared skills")
        return "\n".join(lines)

    def _skills_agent_add(self, args: list[str]) -> str:
        runtime_id = self._require_runtime_id(args)
        skills = self._csv_values(args[1:])
        if not skills:
            raise ValueError("Usage: /skill add <id> <skill1,skill2,...>")
        installed = set(self._discover_shared_skill_names())
        missing = [name for name in skills if name not in installed]
        if missing:
            raise ValueError(f"Skills not found in hub: {', '.join(missing)}")
        return self._update_agent_skills(runtime_id, skills, mode="add")

    def _skills_agent_remove(self, args: list[str]) -> str:
        runtime_id = self._require_runtime_id(args)
        skills = self._csv_values(args[1:])
        if not skills:
            raise ValueError("Usage: /skill remove <id> <skill1,skill2,...>")
        return self._update_agent_skills(runtime_id, skills, mode="remove")

    def _skills_agent_sync(self, args: list[str]) -> str:
        runtime_id = self._require_runtime_id(args)
        all_skills = self._discover_shared_skill_names()
        if not all_skills:
            return f"No skills found in hub ({self._shared_install_root()})"
        return self._update_agent_skills(runtime_id, all_skills, mode="set")

    def _skills_agent_clear(self, args: list[str]) -> str:
        runtime_id = self._require_runtime_id(args)
        return self._update_agent_skills(runtime_id, [], mode="set")

    def _madame_root(self) -> Path:
        return self.workspace.parent

    def _shared_root(self) -> Path:
        return self._madame_root() / "shared"

    def _shared_workdir(self) -> Path:
        return self._shared_root() / "workdir"

    def _shared_install_root(self) -> Path:
        return self._shared_root() / "skills"

    def _ensure_shared_layout(self) -> None:
        self._shared_workdir().mkdir(parents=True, exist_ok=True)

    def _discover_shared_skill_names(self) -> list[str]:
        install_root = self._shared_install_root()
        if not install_root.exists():
            return []
        names: list[str] = []
        for item in sorted(install_root.iterdir(), key=lambda path: path.name):
            if item.name.startswith(".") or not item.is_dir():
                continue
            if (item / "SKILL.md").is_file():
                names.append(item.name)
        return names

    @staticmethod
    def _path_points_to(path: Path, target: Path) -> bool:
        try:
            return path.resolve(strict=False) == target.resolve(strict=False)
        except Exception:
            return False

    def _refresh_shared_skill_state(self) -> dict[str, object]:
        self._ensure_shared_layout()
        install_root = self._shared_install_root()
        installed = self._discover_shared_skill_names()
        return {
            "install_root": install_root,
            "installed": installed,
        }

    
    def _agent_skills_dir(self, agent: ManagedAgent) -> Path:
        return Path(agent.workspace_path).expanduser().resolve() / "skills"

    def _is_managed_workspace_skill(self, path: Path) -> bool:
        return path.is_symlink() and self._path_points_to(path, self._shared_install_root() / path.name)

    def _inspect_agent_skills(self, agent: ManagedAgent) -> tuple[list[str], list[str], list[str]]:
        skills_dir = self._agent_skills_dir(agent)
        linked: list[str] = []
        local_only: list[str] = []
        conflicts: list[str] = []
        if skills_dir.exists():
            for entry in sorted(skills_dir.iterdir(), key=lambda path: path.name):
                if not entry.is_dir() or not (entry / "SKILL.md").is_file():
                    continue
                if self._is_managed_workspace_skill(entry):
                    linked.append(entry.name)
                else:
                    local_only.append(entry.name)
        for name in agent.skills:
            entry = skills_dir / name
            if (entry.exists() or entry.is_symlink()) and not self._is_managed_workspace_skill(entry):
                if name not in conflicts:
                    conflicts.append(name)
        return linked, local_only, conflicts

    def _reconcile_agent_skills(self, agent: ManagedAgent) -> dict[str, object]:
        state = self._refresh_shared_skill_state()
        installed = set(state["installed"])
        desired = {
            name for name in agent.skills
            if name in installed and not agent.archived and agent.mode != "chat"
        }
        skills_dir = self._agent_skills_dir(agent)
        linked: list[str] = []
        removed: list[str] = []
        conflicts: list[str] = []

        if skills_dir.exists():
            for entry in list(skills_dir.iterdir()):
                if not self._is_managed_workspace_skill(entry):
                    continue
                if entry.name not in desired:
                    self._remove_path(entry)
                    removed.append(entry.name)

        if agent.archived or agent.mode == "chat":
            return {
                "linked": linked,
                "removed": removed,
                "conflicts": conflicts,
                "skipped": agent.runtime_id,
            }

        skills_dir.mkdir(parents=True, exist_ok=True)
        install_root = Path(state["install_root"])
        for name in [skill for skill in agent.skills if skill in desired]:
            link_path = skills_dir / name
            target = install_root / name
            if link_path.is_symlink():
                if self._path_points_to(link_path, target):
                    linked.append(name)
                    continue
                if self._is_managed_workspace_skill(link_path):
                    self._remove_path(link_path)
                else:
                    conflicts.append(name)
                    continue
            elif link_path.exists():
                conflicts.append(name)
                continue
            link_path.symlink_to(target, target_is_directory=True)
            linked.append(name)

        return {
            "linked": linked,
            "removed": removed,
            "conflicts": conflicts,
            "skipped": "",
        }

    def _reconcile_all_agent_skills(self, registry) -> list[str]:
        touched: list[str] = []
        for agent in registry.agents:
            result = self._reconcile_agent_skills(agent)
            if result["linked"] or result["removed"] or result["conflicts"]:
                touched.append(agent.runtime_id)
        return touched

    def _update_agent_skills(self, runtime_id: str, skills: list[str], *, mode: str) -> str:
        registry = self.store.load()
        agent = registry.get_agent(runtime_id)
        if agent is None:
            raise ValueError(f"Agent '{runtime_id}' not found in registry.")

        current = list(agent.skills)
        if mode == "set":
            updated = list(skills)
        elif mode == "add":
            updated = self._csv_values([*current, *skills])
        elif mode == "remove":
            removed = set(skills)
            updated = [name for name in current if name not in removed]
        else:
            raise ValueError(f"Unsupported skills update mode '{mode}'.")

        agent.skills = updated
        registry.upsert_agent(agent)
        self.store.save(registry)
        result = self._reconcile_agent_skills(agent)
        return "\n".join(
            [
                f"Updated skills for {runtime_id}:",
                f"- assigned: {', '.join(agent.skills) if agent.skills else '(none)'}",
                f"- linked: {', '.join(result['linked']) if result['linked'] else '(none)'}",
                f"- removed: {', '.join(result['removed']) if result['removed'] else '(none)'}",
                f"- conflicts: {', '.join(result['conflicts']) if result['conflicts'] else '(none)'}",
            ]
        )

    def _cron_store_path(self) -> Path:
        return (self.workspace / "cron" / "jobs.json").resolve()

    def _new_cron_service(self):
        from feibot.cron.service import CronService

        return CronService(self._cron_store_path())

    @staticmethod
    def _parse_bool_token(raw: str, option_name: str) -> bool:
        token = str(raw or "").strip().lower()
        if token in {"1", "true", "yes", "on"}:
            return True
        if token in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"{option_name} must be a boolean value (true|false).")

    def _cron_command(self, args: list[str]) -> str:
        if not args:
            return (
                "Cron commands:\n"
                "/agent cron list [--all true|false]\n"
                "/agent cron add --name <name> "
                "[--message <text> | --exec <command> [--working-dir <path>] | --system-event <name>] "
                "[--every <seconds> | --cron <expr> [--tz <IANA>] | --at <ISO>]\n"
                "/agent cron runs <job_id> [--limit <1..200>]\n"
                "/agent cron remove <job_id>\n"
                "/agent cron enable <job_id>\n"
                "/agent cron disable <job_id>\n"
                "/agent cron run <job_id> [--force true|false]"
            )

        sub = str(args[0] or "").strip().lower()
        rest = args[1:]
        if sub == "list":
            return self._cron_list(rest)
        if sub == "add":
            return self._cron_add(rest)
        if sub == "runs":
            return self._cron_runs(rest)
        if sub == "remove":
            return self._cron_remove(rest)
        if sub == "enable":
            return self._cron_enable_disable(rest, enable=True)
        if sub == "disable":
            return self._cron_enable_disable(rest, enable=False)
        if sub == "run":
            return self._cron_run(rest)
        raise ValueError(f"Unknown cron subcommand '{sub}'.")

    def _cron_list(self, args: list[str]) -> str:
        import time
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo

        include_disabled = False
        if args:
            if args == ["--all"]:
                include_disabled = True
            elif len(args) == 2 and args[0] == "--all":
                include_disabled = self._parse_bool_token(args[1], "--all")
            else:
                raise ValueError("Usage: /agent cron list [--all true|false]")

        service = self._new_cron_service()
        jobs = service.list_jobs(include_disabled=include_disabled)
        if not jobs:
            return "No scheduled jobs."

        lines = [
            "Scheduled jobs:",
            "",
            "| ID | Name | Schedule | Enabled | Run | Business | Delivery | Next Run |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
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

            next_run = "-"
            if job.state.next_run_at_ms:
                ts = job.state.next_run_at_ms / 1000
                try:
                    tz = ZoneInfo(job.schedule.tz) if job.schedule.tz else None
                    next_run = _dt.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    next_run = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))

            enabled = "enabled" if job.enabled else "disabled"
            lines.append(
                "| "
                f"{self._md_cell(job.id)} | "
                f"{self._md_cell(job.name)} | "
                f"{self._md_cell(sched)} | "
                f"{self._md_cell(enabled)} | "
                f"{self._md_cell(job.state.run_status or '-')} | "
                f"{self._md_cell(job.state.business_status or '-')} | "
                f"{self._md_cell(job.state.delivery_status or '-')} | "
                f"{self._md_cell(next_run)} |"
            )
        return "\n".join(lines)

    def _cron_add(self, args: list[str]) -> str:
        import datetime

        from feibot.cron.types import CronSchedule

        options = self._parse_options(args)
        name = str(options.get("name", "")).strip()
        message = str(options.get("message", "")).strip()
        exec_command = str(options.get("exec", "")).strip()
        system_event = str(options.get("system-event", "")).strip()
        working_dir = str(options.get("working-dir", "")).strip() or None
        payload_count = sum(bool(item) for item in (message, exec_command, system_event))
        if not name:
            raise ValueError("cron add requires --name.")
        if payload_count != 1:
            raise ValueError("cron add requires exactly one of --message, --exec, or --system-event.")

        every_raw = str(options.get("every", "")).strip()
        cron_expr = str(options.get("cron", "")).strip()
        tz = str(options.get("tz", "")).strip() or None
        at_raw = str(options.get("at", "")).strip()
        notify_policy = str(options.get("notify-policy", "changes_only") or "").strip().lower()
        if notify_policy not in {"always", "changes_only", "digest"}:
            raise ValueError("--notify-policy must be always|changes_only|digest")
        notify_on_error = self._parse_bool_token(
            str(options.get("notify-on-error", "true")),
            "--notify-on-error",
        )
        to = str(options.get("to", "")).strip() or None
        channel = str(options.get("channel", "")).strip() or None

        if tz and not cron_expr:
            raise ValueError("--tz can only be used with --cron")
        if channel and channel != "feishu":
            raise ValueError("--channel only supports 'feishu'")
        if to and not channel:
            channel = "feishu"
        if working_dir and not exec_command:
            raise ValueError("--working-dir can only be used with --exec")

        if every_raw:
            try:
                every_s = int(every_raw)
            except ValueError as exc:
                raise ValueError(f"invalid --every value '{every_raw}'") from exc
            if every_s <= 0:
                raise ValueError("--every must be > 0")
            schedule = CronSchedule(kind="every", every_ms=every_s * 1000)
        elif cron_expr:
            schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz)
        elif at_raw:
            try:
                dt = datetime.datetime.fromisoformat(at_raw)
            except ValueError as exc:
                raise ValueError(f"invalid --at datetime '{at_raw}': {exc}") from exc
            schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
        else:
            raise ValueError("Must specify --every, --cron, or --at")

        payload_kind: str
        payload_message = message
        payload_command: str | None = None
        if exec_command:
            payload_kind = "exec"
            payload_message = ""
            payload_command = exec_command
        elif system_event:
            payload_kind = "system_event"
            payload_message = system_event
        else:
            payload_kind = "agent_turn"

        service = self._new_cron_service()
        job, status = service.upsert_job(
            name=name,
            schedule=schedule,
            message=payload_message,
            payload_kind=payload_kind,  # type: ignore[arg-type]
            notify_policy=notify_policy,  # type: ignore[arg-type]
            notify_on_error=notify_on_error,
            to=to,
            channel=channel,
            delete_after_run=(schedule.kind == "at"),
            command=payload_command,
            working_dir=working_dir,
        )
        if status == "created":
            return f"Added job '{job.name}' ({job.id})"
        if status == "updated":
            return f"Updated job '{job.name}' ({job.id})"
        return f"Job unchanged '{job.name}' ({job.id})"

    def _cron_runs(self, args: list[str]) -> str:
        from datetime import datetime as _dt

        if not args:
            raise ValueError("Usage: /agent cron runs <job_id> [--limit <1..200>]")
        job_id = str(args[0] or "").strip()
        if not job_id:
            raise ValueError("job_id cannot be empty.")

        limit = 20
        if len(args) > 1:
            options = self._parse_options(args[1:])
            limit_raw = str(options.get("limit", "20")).strip()
            try:
                limit = int(limit_raw)
            except ValueError as exc:
                raise ValueError(f"invalid --limit value '{limit_raw}'") from exc
        if limit < 1 or limit > 200:
            raise ValueError("--limit must be between 1 and 200")

        service = self._new_cron_service()
        entries = service.list_runs(job_id, limit=limit)
        if not entries:
            return f"No run logs for job {job_id}."

        lines = [
            f"Cron runs ({job_id}):",
            "",
            "| Time | Run | Business | Delivery | Summary/Error |",
            "| --- | --- | --- | --- | --- |",
        ]
        for entry in entries:
            ts = entry.get("ts")
            when = (
                _dt.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
                if isinstance(ts, (int, float))
                else "-"
            )
            summary_or_error = str(entry.get("summary") or entry.get("error") or "-")
            lines.append(
                "| "
                f"{self._md_cell(when)} | "
                f"{self._md_cell(entry.get('runStatus') or '-')} | "
                f"{self._md_cell(entry.get('businessStatus') or '-')} | "
                f"{self._md_cell(entry.get('deliveryStatus') or '-')} | "
                f"{self._md_cell(summary_or_error[:1000])} |"
            )
        return "\n".join(lines)

    def _cron_remove(self, args: list[str]) -> str:
        if not args:
            raise ValueError("Usage: /agent cron remove <job_id>")
        job_id = str(args[0] or "").strip()
        if not job_id:
            raise ValueError("job_id cannot be empty.")

        service = self._new_cron_service()
        if service.remove_job(job_id):
            return f"Removed job {job_id}"
        return f"Job {job_id} not found"

    def _cron_enable_disable(self, args: list[str], *, enable: bool) -> str:
        if not args:
            action = "enable" if enable else "disable"
            raise ValueError(f"Usage: /agent cron {action} <job_id>")
        job_id = str(args[0] or "").strip()
        if not job_id:
            raise ValueError("job_id cannot be empty.")

        service = self._new_cron_service()
        job = service.enable_job(job_id, enabled=enable)
        if job is None:
            return f"Job {job_id} not found"
        status = "enabled" if enable else "disabled"
        return f"Job '{job.name}' {status}"

    def _cron_run(self, args: list[str]) -> str:
        if not args:
            raise ValueError("Usage: /agent cron run <job_id> [--force true|false]")
        job_id = str(args[0] or "").strip()
        if not job_id:
            raise ValueError("job_id cannot be empty.")

        force = False
        if len(args) > 1:
            if args[1:] == ["--force"]:
                force = True
            else:
                options = self._parse_options(args[1:])
                if "force" in options:
                    force = self._parse_bool_token(str(options.get("force", "false")), "--force")

        ran = False
        if self._runtime_loop is not None and self._runtime_cron_service is not None:
            future = asyncio.run_coroutine_threadsafe(
                self._runtime_cron_service.run_job(job_id, force=force),
                self._runtime_loop,
            )
            ran = bool(future.result(timeout=180))
        else:
            service = self._new_cron_service()
            ran = bool(asyncio.run(service.run_job(job_id, force=force)))

        if not ran:
            return f"Failed to run job {job_id}"

        service = self._new_cron_service()
        entries = service.list_runs(job_id, limit=1)
        if not entries:
            return "Job executed"
        latest = entries[0]
        return (
            "Job executed\n"
            f"run={latest.get('runStatus') or '-'} "
            f"business={latest.get('businessStatus') or '-'} "
            f"delivery={latest.get('deliveryStatus') or '-'} "
            f"summary={str(latest.get('summary') or latest.get('error') or '').strip()[:200]}"
        )

    @staticmethod
    def _require_runtime_id(args: list[str]) -> str:
        runtime_id = str(args[0] if args else "").strip()
        if not runtime_id:
            raise ValueError("Missing runtime_id argument.")
        return runtime_id

    def _render_base_dir(self, runtime_id: str) -> str:
        if not self.base_dir_template:
            raise ValueError("Madame base_dir_template is empty. Configure madame.baseDirTemplate first.")
        rendered = self.base_dir_template.format(runtime_id=runtime_id)
        base_path = Path(rendered).expanduser()
        if not base_path.is_absolute():
            base_path = (self.workspace / base_path).resolve()
        return str(base_path)

    @staticmethod
    def _normalize_id(name: str) -> str:
        # Take first word (split on whitespace or CamelCase), lowercase, strip non-alphanumeric.
        name = str(name or "").strip()
        parts = name.split()
        if len(parts) > 1:
            first = parts[0]
        else:
            camel_parts = re.findall(r"[A-Z][a-z]*", name)
            first = camel_parts[0] if camel_parts else name
        # Lowercase first, then strip non-alphanumeric
        return re.sub(r"[^a-z0-9]+", "", first.lower())

    def _launchd_state(self, label: str) -> str:
        launchctl = shutil.which("launchctl")
        if not launchctl:
            return "unavailable"
        domain = f"gui/{os.getuid()}/{label}"
        try:
            proc = subprocess.run(
                [launchctl, "print", domain],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as e:
            return f"error:{e}"

        if proc.returncode != 0:
            return "not_loaded"

        text = proc.stdout or ""
        state_match = re.search(r"\bstate\s*=\s*([A-Za-z_]+)", text)
        pid_match = re.search(r"\bpid\s*=\s*(\d+)", text)
        state = state_match.group(1) if state_match else "loaded"
        pid = pid_match.group(1) if pid_match else ""
        return f"{state}(pid={pid})" if pid else state

    def _run_manage_script(self, agent: ManagedAgent, action: str) -> str:
        if self.manage_script is None:
            raise ValueError(
                "Madame manage script path is not configured or file is missing. "
                "Set madame.manageScript to a valid executable script path."
            )

        cmd = [
            str(self.manage_script),
            "-r",
            str(self.repo_dir),
            "-l",
            agent.launchd_label,
            "-c",
            agent.config_path,
            "-d",
            agent.run_dir,
            action,
        ]
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        merged = output
        if err:
            merged = f"{merged}\n{err}".strip()
        if proc.returncode != 0:
            raise ValueError(f"madame_ops {action} failed (exit={proc.returncode}):\n{merged}")
        return merged or f"madame_ops {action} succeeded."

    def _backup_agent_files(self, agent: ManagedAgent) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        archive_path = self.backup_dir / f"{agent.runtime_id}-{timestamp}.tar.gz"

        candidate_paths = [
            Path(agent.workspace_path),
            Path(agent.sessions_path) if agent.sessions_path else None,
            Path(agent.run_dir),
            Path(agent.config_path),
        ]

        with tarfile.open(archive_path, "w:gz") as tar:
            for path in candidate_paths:
                if path is None:
                    continue
                resolved = path.expanduser().resolve()
                if not resolved.exists():
                    continue
                tar.add(str(resolved), arcname=f"{agent.runtime_id}/{resolved.name}")

        for path in candidate_paths:
            if path is None:
                continue
            self._remove_path(path)

        return archive_path

    @staticmethod
    def _remove_path(path: Path) -> None:
        candidate = Path(path).expanduser()
        if not candidate.exists() and not candidate.is_symlink():
            return
        if candidate.is_file() or candidate.is_symlink():
            candidate.unlink(missing_ok=True)
            return
        shutil.rmtree(candidate)

    def _init_agent_files(
        self,
        *,
        agent: ManagedAgent,
        workspace_path: Path,
        sessions_path: Path,
        run_dir: Path,
        config_path: Path,
        model: str,
        allow_from: list[str],
        providers: dict[str, object],
        tool_defaults: dict[str, object] | None = None,
    ) -> None:
        workspace_path = workspace_path.expanduser().resolve()
        sessions_path = sessions_path.expanduser().resolve()
        run_dir = run_dir.expanduser().resolve()
        config_path = config_path.expanduser().resolve()

        workspace_path.mkdir(parents=True, exist_ok=True)
        sessions_path.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(parents=True, exist_ok=True)

        # Only create full workspace layout for agent role (not chater)
        if agent.role == "agent":
            (workspace_path / "skills").mkdir(parents=True, exist_ok=True)
            (workspace_path / "memory").mkdir(parents=True, exist_ok=True)
            memory_md = workspace_path / "memory" / "MEMORY.md"
            if not memory_md.exists():
                memory_md.write_text("# Long-term Memory\n", encoding="utf-8")
            history_md = workspace_path / "memory" / "HISTORY.md"
            if not history_md.exists():
                history_md.write_text("", encoding="utf-8")

        agents_md = workspace_path / "AGENTS.md"
        if not agents_md.exists():
            agents_content = self._render_agents_template(
                runtime_id=agent.runtime_id,
                role=agent.role,
                workspace_path=str(workspace_path),
                registry_path=str(self.store.path),
                shared_workdir=str(self._shared_workdir()),
            )
            agents_md.write_text(agents_content, encoding="utf-8")

        config_payload = self._build_config_payload(
            agent=agent,
            model=model,
            sessions_path=sessions_path,
            workspace_path=workspace_path,
            allow_from=allow_from,
            providers=providers,
            tool_defaults=tool_defaults,
        )
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(config_payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _render_agents_template(
        self,
        runtime_id: str,
        role: str,
        workspace_path: str,
        registry_path: str = "",
        shared_workdir: str = "",
    ) -> str:
        """Render the AGENTS.md template with role-specific content."""
        if role == "manager":
            return f"""# Madame - Control Plane Agent

You are **Madame**, the control plane agent that manages a team of specialized agents.

## Your Responsibilities

- Manage and coordinate a team of agents (create, start, stop, restart, archive)
- Route requests to the appropriate specialized agent based on the task
- Monitor agent health and manage the credential pool
- Maintain the agent registry at: `{registry_path}`

## Managed Agents

Agents are registered in the registry file. Use `/agent list` to see all managed agents.

## Available Commands

- `/agent list` - List all managed agents
- `/agent create --name <id> --mode agent|chat` - Create a new agent
- `/agent start|stop|restart <id>` - Lifecycle control
- `/agent pool list|add|remove` - Manage credential slots
- `/agent skills ...` - Manage shared skills and per-agent links

## Guidelines

- Be concise and accurate
- Always explain actions before taking them
- Ask for clarification when requests are ambiguous
"""
        elif role == "chater":
            return f"""# Chat Agent

You are **{runtime_id}**, a lightweight chat agent.

## Mode

You are in chat mode - a streamlined experience for casual conversation.

## Guidelines

- Be friendly and helpful
- Keep responses concise
- Use web search/fetch for factual questions
"""
        else:
            # agent role - full workspace guide
            return f"""# Agent Instructions

You are **{runtime_id}**, a full-featured agent with tools, memory, and skills.

## Workspace

Your private workspace is rooted at `$FEIBOT_AGENT_BASE_DIR/workspace`.

You also have a shared collaboration root at `$FEIBOT_SHARED_WORKDIR`.

Prefer shared locations first for stable, reusable, or collaborative work:

- `projects/`
- `github/`
- `tasks/`
- `data/`
- `scripts/`

If work fits one of those categories and may be reused by other agents, place it under the matching directory in `$FEIBOT_SHARED_WORKDIR`.

Keep these local to your own workspace:

- `memory/`
- `skills/`
- `cron/`
- `sessions/`
- `downloads/`
- `logs/`
- `cache/`
- `tmp/`
- runtime files
- envs
- secrets and private notes

Use the local workspace for private, transient, or agent-specific work. Before inventing a new top-level directory, prefer the existing structure above.

## Memory

- `memory/MEMORY.md` is approved long-term context. Do not modify it without explicit user approval.
- `memory/HISTORY.md` is a searchable session index. Search it only when the user asks about prior work or past sessions.

## Skills

Skills live under `skills/<skill-name>/SKILL.md`.

Treat local workspace skills as agent-private by default unless they are intentionally promoted into the shared skill system managed by Madame.

## Guidelines

- Be concise, accurate, and explicit about tradeoffs.
- Explain what you are doing before taking actions.
- Ask for clarification when the request is ambiguous.
- Use tools to accomplish tasks.
- Prefer the existing workspace structure over inventing new folders.
"""

    def _build_config_payload(
        self,
        *,
        agent: ManagedAgent,
        model: str,
        sessions_path: Path,
        workspace_path: Path,
        allow_from: list[str],
        providers: dict[str, object],
        tool_defaults: dict[str, object] | None = None,
    ) -> dict[str, object]:
        chat_mode = agent.mode == "chat"
        madame_mode = agent.role == "manager" or agent.profile == "manager"
        disabled_tools = [name for name in KNOWN_TOOL_NAMES if name not in CHAT_ALLOWED_TOOLS] if chat_mode else []
        raw_tools = deepcopy(tool_defaults) if isinstance(tool_defaults, dict) else {}
        writable_dirs = self._normalize_writable_dirs(raw_tools.get("writableDirs"))
        allowed_hosts = self._normalize_allowed_hosts(raw_tools.get("allowedHosts"))
        base_dir = str(workspace_path.expanduser().resolve().parent)
        shared_workdir = str(self._shared_workdir())
        effective_writable_dirs = self._csv_values(
            [str(workspace_path.expanduser().resolve()), *writable_dirs, shared_workdir]
        )
        skills_env = {
            "FEIBOT_AGENT_BASE_DIR": base_dir,
            "FEIBOT_SHARED_WORKDIR": shared_workdir,
        }
        if not madame_mode:
            skills_env["FEIBOT_DISABLE_BUILTIN_SKILLS"] = "1"

        return {
            "name": agent.runtime_id,
            "paths": {
                "workspace": str(workspace_path),
                "sessions": str(sessions_path),
            },
            "agents": {
                "defaults": {
                    "model": model,
                    "provider": "auto",
                    "maxTokens": 8192,
                    "temperature": 0.7,
                    "maxToolIterations": 100,
                    "maxConsecutiveToolErrors": 10,
                    "memoryWindow": 0 if chat_mode else 50,
                    # Keep tools enabled and whitelist via tools.disabledTools for chat mode.
                    "disableTools": False,
                    "disableSkills": chat_mode,
                    "disableLongTermMemory": chat_mode,
                }
            },
            "providers": deepcopy(providers),
            "channels": {
                "feishu": {
                    "enabled": True,
                    "appId": agent.app_id,
                    "appSecret": agent.app_secret,
                    "allowFrom": allow_from,
                }
            },
            "tools": {
                "writableDirs": effective_writable_dirs,
                "allowedHosts": allowed_hosts,
                "disabledTools": disabled_tools,
                "exec": {},
            },
            "madame": {
                "enabled": madame_mode,
                "runtimeId": agent.runtime_id if madame_mode else self.madame_runtime_id,
                "registryPath": str(self.store.path),
                "manageScript": str(self.manage_script) if self.manage_script else "",
                "baseDirTemplate": self.base_dir_template,
                "backupDir": str(self.backup_dir),
                "enforceIsolation": True,
            },
            "skills": {
                "env": skills_env
            },
        }

    def _manager_config_paths(self, registry) -> list[Path]:
        candidates: list[ManagedAgent] = []
        primary = registry.get_agent(self.madame_runtime_id)
        if primary is not None:
            candidates.append(primary)
        for agent in registry.agents:
            if agent in candidates:
                continue
            if agent.role == "manager" or agent.profile == "manager":
                candidates.append(agent)

        paths: list[Path] = []
        seen: set[str] = set()
        for manager_agent in candidates:
            path_raw = str(manager_agent.config_path or "").strip()
            if not path_raw:
                continue
            cfg_path = Path(path_raw).expanduser().resolve()
            key = str(cfg_path)
            if key in seen:
                continue
            seen.add(key)
            paths.append(cfg_path)

        fallback = (self.store.path.parent / "config.json").expanduser().resolve()
        fallback_key = str(fallback)
        if fallback_key not in seen:
            paths.append(fallback)
        return paths

    @staticmethod
    def _normalize_writable_dirs(raw: object) -> list[str]:
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for item in raw:
            text = str(item or "").strip()
            if not text:
                continue
            resolved = str(Path(text).expanduser().resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            out.append(resolved)
        return out

    @staticmethod
    def _normalize_allowed_hosts(raw: object) -> list[str]:
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for item in raw:
            text = str(item or "").strip().lower()
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(text)
        return out

    def _resolve_tool_defaults(self, registry) -> dict[str, object]:
        for cfg_path in self._manager_config_paths(registry):
            if not cfg_path.exists() or not cfg_path.is_file():
                continue
            try:
                raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            tools = raw.get("tools")
            if isinstance(tools, dict):
                resolved = deepcopy(tools)
                manager_workspace = Path(
                    str(((raw.get("paths") or {}).get("workspace") or cfg_path.parent / "workspace"))
                ).expanduser().resolve()
                manager_base_dir = str(manager_workspace.parent)
                raw_writable = resolved.get("writableDirs")
                if isinstance(raw_writable, list):
                    resolved["writableDirs"] = [
                        item
                        for item in self._normalize_writable_dirs(raw_writable)
                        if item not in {manager_base_dir, str(manager_workspace)}
                    ]
                raw_hosts = resolved.get("allowedHosts")
                if isinstance(raw_hosts, list):
                    resolved["allowedHosts"] = self._normalize_allowed_hosts(raw_hosts)
                return resolved
        return {}

    def _resolve_provider_defaults(self, registry) -> dict[str, object]:
        errors: list[str] = []
        for cfg_path in self._manager_config_paths(registry):
            if not cfg_path.exists() or not cfg_path.is_file():
                errors.append(f"{cfg_path} (missing file)")
                continue
            try:
                raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                errors.append(f"{cfg_path} (invalid json)")
                continue
            providers = raw.get("providers")
            if isinstance(providers, dict) and providers:
                return deepcopy(providers)
            errors.append(f"{cfg_path} (providers missing/empty)")

        if errors:
            detail = "; ".join(errors[:3])
            raise ValueError(f"Madame providers are required for /agent create: {detail}")
        raise ValueError("Madame providers are required for /agent create, but manager config is not found.")

    def _resolve_default_model(self, registry) -> str:
        errors: list[str] = []
        for cfg_path in self._manager_config_paths(registry):
            if not cfg_path.exists() or not cfg_path.is_file():
                errors.append(f"{cfg_path} (missing file)")
                continue
            try:
                raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                errors.append(f"{cfg_path} (invalid json)")
                continue
            model = str(((raw.get("agents") or {}).get("defaults") or {}).get("model") or "").strip()
            if model:
                return model
            errors.append(f"{cfg_path} (agents.defaults.model missing)")

        if errors:
            detail = "; ".join(errors[:3])
            raise ValueError(f"Madame default model is required for /agent create: {detail}")
        raise ValueError("Madame default model is required for /agent create, but manager config is not found.")

    @staticmethod
    def _csv_values(raw_values: list[str] | str) -> list[str]:
        if isinstance(raw_values, str):
            values = [raw_values]
        else:
            values = list(raw_values)
        out: list[str] = []
        seen: set[str] = set()
        for raw in values:
            for token in str(raw or "").split(","):
                item = token.strip()
                if not item or item in seen:
                    continue
                seen.add(item)
                out.append(item)
        return out

    @staticmethod
    def _role_defaults(role: str) -> dict[str, str]:
        role = str(role or "").strip().lower()
        if role == "manager":
            return {"tool_policy": "madame_only", "memory_mode": "default", "skill_mode": "default"}
        if role == "chater":
            return {"tool_policy": "no_tools", "memory_mode": "disabled", "skill_mode": "disabled"}
        return {"tool_policy": "default", "memory_mode": "default", "skill_mode": "default"}

    class _ParsedOptions(dict):
        def get_all(self, key: str) -> list[str]:
            value = self.get(key)
            if value is None:
                return []
            if isinstance(value, list):
                return [str(x) for x in value]
            return [str(value)]

    @classmethod
    def _parse_options(cls, tokens: list[str]) -> "_ParsedOptions":
        out: AgentMadameController._ParsedOptions = cls._ParsedOptions()
        idx = 0
        while idx < len(tokens):
            token = tokens[idx]
            if not token.startswith("--"):
                raise ValueError(f"Unexpected token: {token}")
            key = token[2:].strip()
            if not key:
                raise ValueError("Invalid option '--'.")
            if idx + 1 >= len(tokens):
                raise ValueError(f"Missing value for option --{key}.")
            value = tokens[idx + 1]
            if key in {"shared-skill-group", "exclusive-skill"}:
                existing = out.get(key)
                if isinstance(existing, list):
                    existing.append(value)
                elif existing is None:
                    out[key] = [value]
                else:
                    out[key] = [str(existing), value]
            else:
                out[key] = value
            idx += 2
        return out
