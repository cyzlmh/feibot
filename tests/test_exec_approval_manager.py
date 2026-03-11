from datetime import datetime

from feibot.agent.exec_approval import ExecApprovalManager


def test_normalize_decision_aliases() -> None:
    assert ExecApprovalManager.normalize_decision("allow") == "allow-once"
    assert ExecApprovalManager.normalize_decision("once") == "allow-once"
    assert ExecApprovalManager.normalize_decision("always") is None
    assert ExecApprovalManager.normalize_decision("reject") == "deny"
    assert ExecApprovalManager.normalize_decision("unknown") is None


def test_requester_only_approval_and_allow_once() -> None:
    manager = ExecApprovalManager(enabled=True)
    request = manager.create_request(
        command="rm -f cache.txt",
        working_dir="/workspace",
        channel="feishu",
        chat_id="oc_1",
        session_key="feishu:oc_1",
        requester_id="ou_requester",
        risk_level="dangerous",
    )
    assert request.risk_level == "dangerous"

    denied, denied_err = manager.resolve(
        approval_id=request.id,
        decision="allow-once",
        resolved_by="ou_other",
    )
    assert denied is None
    assert "not authorized" in denied_err
    assert manager.get_request(request.id) is not None

    resolved, err = manager.resolve(
        approval_id=request.id,
        decision="allow-once",
        resolved_by="ou_requester",
    )
    assert err == ""
    assert resolved is not None
    assert resolved.decision == "allow-once"
    assert resolved.request.risk_level == "dangerous"
    assert manager.get_request(request.id) is None

def test_configured_approver_can_resolve_others_request() -> None:
    manager = ExecApprovalManager(
        enabled=True,
        approvers=["ou_admin"],
    )
    request = manager.create_request(
        command="rm -f cache.txt",
        working_dir="/workspace",
        channel="feishu",
        chat_id="oc_1",
        session_key="feishu:oc_1",
        requester_id="ou_requester",
    )
    resolved, err = manager.resolve(
        approval_id=request.id,
        decision="allow-once",
        resolved_by="ou_admin",
    )
    assert err == ""
    assert resolved is not None
    assert manager.get_request(request.id) is None


def test_missing_request_returns_not_found() -> None:
    manager = ExecApprovalManager(enabled=True)
    request = manager.create_request(
        command="rm -f old.txt",
        working_dir="/workspace",
        channel="feishu",
        chat_id="oc_1",
        session_key="feishu:oc_1",
        requester_id="ou_requester",
    )
    assert request.created_at <= datetime.now()

    resolved, err = manager.resolve(
        approval_id="missing",
        decision="allow-once",
        resolved_by="ou_requester",
    )
    assert resolved is None
    assert "not found" in err
