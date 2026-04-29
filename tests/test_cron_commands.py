import json
from pathlib import Path

from feibot.madame.controller import AgentMadameController


def _make_controller(tmp_path: Path) -> AgentMadameController:
    workspace = tmp_path / "madame-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    return AgentMadameController(
        workspace=workspace,
        repo_dir=repo_dir,
        registry_path=tmp_path / "madame" / "agents_registry.json",
        madame_runtime_id="madame",
        manage_script=None,
        base_dir_template=str(tmp_path / "agents" / "{runtime_id}"),
    )


def test_agent_cron_add_rejects_invalid_timezone(tmp_path) -> None:
    controller = _make_controller(tmp_path)

    reply = controller.execute(
        'cron add --name demo --message hello --cron "0 9 * * *" --tz America/Vancovuer'
    )

    assert "Error: unknown timezone 'America/Vancovuer'" in reply


def test_agent_cron_add_creates_job_file(tmp_path) -> None:
    controller = _make_controller(tmp_path)

    reply = controller.execute(
        "cron add --name demo --message hello --every 60"
    )

    assert "Added job 'demo'" in reply
    jobs_path = tmp_path / "madame-workspace" / "cron" / "jobs.json"
    assert jobs_path.exists()
    payload = json.loads(jobs_path.read_text(encoding="utf-8"))
    assert any(job["name"] == "demo" for job in payload["jobs"])


def test_agent_cron_add_exec_job_records_command_and_working_dir(tmp_path) -> None:
    controller = _make_controller(tmp_path)

    reply = controller.execute(
        'cron add --name crypto-monitor --exec "uv run scripts/price_monitor.py --threshold 5 --structured" '
        "--working-dir /tmp/crypto-skill --cron '0 */4 * * *'"
    )

    assert "Added job 'crypto-monitor'" in reply
    jobs_path = tmp_path / "madame-workspace" / "cron" / "jobs.json"
    payload = json.loads(jobs_path.read_text(encoding="utf-8"))
    job = next(job for job in payload["jobs"] if job["name"] == "crypto-monitor")
    assert job["payload"]["kind"] == "exec"
    assert job["payload"]["message"] == ""
    assert job["payload"]["command"] == "uv run scripts/price_monitor.py --threshold 5 --structured"
    assert job["payload"]["workingDir"] == "/tmp/crypto-skill"


def test_agent_cron_add_system_event_job_records_kind(tmp_path) -> None:
    controller = _make_controller(tmp_path)

    reply = controller.execute(
        "cron add --name nightly-history --system-event history_sync --cron '0 4 * * *'"
    )

    assert "Added job 'nightly-history'" in reply
    jobs_path = tmp_path / "madame-workspace" / "cron" / "jobs.json"
    payload = json.loads(jobs_path.read_text(encoding="utf-8"))
    job = next(job for job in payload["jobs"] if job["name"] == "nightly-history")
    assert job["payload"]["kind"] == "system_event"
    assert job["payload"]["message"] == "history_sync"
    assert job["payload"]["command"] is None


def test_agent_cron_add_notify_policy_defaults_to_feishu_channel(tmp_path) -> None:
    controller = _make_controller(tmp_path)

    reply = controller.execute(
        "cron add --name deliver-demo --message hello --every 60 "
        "--notify-policy always --to oc_test_chat"
    )

    assert "Added job 'deliver-demo'" in reply
    jobs_path = tmp_path / "madame-workspace" / "cron" / "jobs.json"
    payload = json.loads(jobs_path.read_text(encoding="utf-8"))
    job = next(job for job in payload["jobs"] if job["name"] == "deliver-demo")
    assert job["payload"]["channel"] == "feishu"
    assert job["payload"]["to"] == "oc_test_chat"
    assert job["payload"]["notifyPolicy"] == "always"


def test_agent_cron_add_default_policy_can_use_runtime_default_target(tmp_path) -> None:
    controller = _make_controller(tmp_path)

    reply = controller.execute(
        "cron add --name deliver-missing --message hello --every 60"
    )

    assert "Added job 'deliver-missing'" in reply
    jobs_path = tmp_path / "madame-workspace" / "cron" / "jobs.json"
    payload = json.loads(jobs_path.read_text(encoding="utf-8"))
    job = next(job for job in payload["jobs"] if job["name"] == "deliver-missing")
    assert job["payload"]["notifyPolicy"] == "changes_only"
    assert job["payload"]["channel"] is None
    assert job["payload"]["to"] is None


def test_agent_cron_add_rejects_non_feishu_channel(tmp_path) -> None:
    controller = _make_controller(tmp_path)

    reply = controller.execute(
        "cron add --name invalid-channel --message hello --every 60 --channel cli"
    )

    assert "Error: --channel only supports 'feishu'" in reply


def test_agent_cron_add_rejects_multiple_payload_modes(tmp_path) -> None:
    controller = _make_controller(tmp_path)

    reply = controller.execute(
        "cron add --name invalid --message hello --exec 'echo hi' --every 60"
    )

    assert "exactly one of --message, --exec, or --system-event" in reply
