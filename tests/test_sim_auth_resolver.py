import asyncio
from datetime import datetime, timedelta

import pytest

from feibot.agent.exec_approval import ExecApprovalRequest
from feibot.agent.sim_auth import SimAuthResolver

_TEST_PRIVATE_KEY = (
    "MIGTAgEAMBMGByqGSM49AgEGCCqBHM9VAYItBHkwdwIBAQQgOFGAgHT0K63hp42fbC4HRxibNMqZgBIs4wp/"
    "wBvTTwGgCgYIKoEcz1UBgi2hRANCAASuANbhTM4R2/st2HBFHdz5BjPT6aZc4j+Yp7KFqvZANSn0KI1G+Rs4XvN"
    "tFYtjZ/Vq8jGxECG9xwEY9bfuz071no"
)


def _make_request() -> ExecApprovalRequest:
    return ExecApprovalRequest(
        id="07951513db",
        command="rm -f /tmp/x",
        working_dir="/tmp",
        channel="feishu",
        chat_id="oc_test",
        session_key="feishu:oc_test",
        requester_id="ou_test",
        created_at=datetime.now(),
        expires_at=datetime.now() + timedelta(minutes=2),
    )


def test_sim_auth_status_zero_maps_to_allow_once() -> None:
    resolver = SimAuthResolver(verify_url="https://sim-auth.local/verify")
    decision, reason = resolver._parse_decision({"status": "0", "resultDesc": "Approved"})
    assert decision == "allow-once"
    assert reason == "Approved"


def test_sim_auth_status_one_maps_to_deny() -> None:
    resolver = SimAuthResolver(verify_url="https://sim-auth.local/verify")
    decision, reason = resolver._parse_decision({"status": "1", "resultDesc": "Canceled"})
    assert decision == "deny"
    assert reason == "Canceled"


def test_sim_auth_nested_status_maps_to_deny() -> None:
    resolver = SimAuthResolver(verify_url="https://sim-auth.local/verify")
    decision, reason = resolver._parse_decision({"data": {"status": "3", "resultDesc": "PIN locked"}})
    assert decision == "deny"
    assert reason == "PIN locked"


def test_sim_auth_cmcc_callback_result_code_maps_to_allow_once() -> None:
    resolver = SimAuthResolver(verify_url="https://sim-auth.local/verify")
    decision, reason = resolver._parse_decision(
        {
            "resultCode": "200",
            "resultDesc": "处理成功",
            "data": {
                "taskId": "MS2026030515290224eec9",
                "callbackResultCode": "0",
                "callbackResultDesc": "成功",
            },
        }
    )
    assert decision == "allow-once"
    assert reason == "成功"


def test_sim_auth_cmcc_callback_result_code_nonzero_maps_to_deny() -> None:
    resolver = SimAuthResolver(verify_url="https://sim-auth.local/verify")
    decision, reason = resolver._parse_decision(
        {
            "taskId": "MS2026030515290224eec9",
            "callbackResultCode": "1005",
            "callbackResultDesc": "认证失败",
        }
    )
    assert decision == "deny"
    assert reason == "认证失败"


def test_sim_auth_approved_boolean_takes_priority() -> None:
    resolver = SimAuthResolver(verify_url="https://sim-auth.local/verify")
    decision, reason = resolver._parse_decision(
        {"approved": True, "status": "-1", "message": "Manual override"}
    )
    assert decision == "allow-once"
    assert reason == "Manual override"


@pytest.mark.asyncio
async def test_sim_auth_verify_surfaces_result_desc_when_decision_missing(monkeypatch) -> None:
    class _FakeResponse:
        status_code = 200
        text = '{"resultCode":"AC1000017","resultDesc":"taskId:不能为空"}'

        @staticmethod
        def json() -> dict[str, str]:
            return {"resultCode": "AC1000017", "resultDesc": "taskId:不能为空"}

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
            return None

        async def post(self, *args, **kwargs) -> _FakeResponse:  # noqa: ANN002, ANN003
            return _FakeResponse()

    monkeypatch.setattr("feibot.agent.sim_auth.httpx.AsyncClient", _FakeClient)

    resolver = SimAuthResolver(verify_url="https://sim-auth.local/verify")
    verdict = await resolver.verify(_make_request())
    assert verdict.decision == "deny"
    assert "taskId:不能为空" in verdict.reason


@pytest.mark.asyncio
async def test_cmcc_verify_send_auth_then_poll_get_result(monkeypatch) -> None:
    resolver = SimAuthResolver(
        cmcc_host="https://ptest.cmccsim.com:9090",
        cmcc_send_auth_path="/trustedAuth/api/simAuth/sendAuth",
        cmcc_get_result_path="/trustedAuth/api/simAuth/getSimAuthResult",
        cmcc_ap_id="A0003",
        cmcc_app_id="A0003001",
        cmcc_private_key=_TEST_PRIVATE_KEY,
        cmcc_msisdn="19802025093",
        cmcc_template_id="DF20240419093514451c32",
        cmcc_poll_interval_sec=0.01,
        cmcc_poll_timeout_sec=1,
        cmcc_callback_timeout_sec=0,
    )
    calls: dict[str, int] = {"result": 0}

    async def _fake_cmcc_post(endpoint: str, payload: dict[str, object]) -> dict[str, object]:
        if endpoint.endswith("/sendAuth"):
            return {"data": {"taskId": "task_1"}}
        calls["result"] += 1
        if calls["result"] == 1:
            return {"resultCode": "AC1000000", "resultDesc": "pending"}
        return {"status": "0", "resultDesc": "Approved"}

    monkeypatch.setattr(resolver, "_cmcc_post", _fake_cmcc_post)

    verdict = await resolver.verify(_make_request())
    assert verdict.decision == "allow-once"
    assert "Approved" in verdict.reason


@pytest.mark.asyncio
async def test_cmcc_verify_poll_real_callback_result_shape(monkeypatch) -> None:
    resolver = SimAuthResolver(
        cmcc_host="https://ptest.cmccsim.com:9090",
        cmcc_send_auth_path="/trustedAuth/api/simAuth/sendAuth",
        cmcc_get_result_path="/trustedAuth/api/simAuth/getSimAuthResult",
        cmcc_ap_id="A0003",
        cmcc_app_id="A0003001",
        cmcc_private_key=_TEST_PRIVATE_KEY,
        cmcc_msisdn="19802025093",
        cmcc_template_id="DF20240419093514451c32",
        cmcc_poll_interval_sec=0.01,
        cmcc_poll_timeout_sec=1,
        cmcc_callback_timeout_sec=0,
    )

    async def _fake_cmcc_post(endpoint: str, _payload: dict[str, object]) -> dict[str, object]:
        if endpoint.endswith("/sendAuth"):
            return {"data": {"taskId": "task_real_shape_1"}}
        return {
            "resultCode": "200",
            "resultDesc": "处理成功",
            "data": {"callbackResultCode": "0", "callbackResultDesc": "成功"},
        }

    monkeypatch.setattr(resolver, "_cmcc_post", _fake_cmcc_post)

    verdict = await resolver.verify(_make_request())
    assert verdict.decision == "allow-once"
    assert "成功" in verdict.reason


@pytest.mark.asyncio
async def test_cmcc_verify_timeout_returns_deny(monkeypatch) -> None:
    resolver = SimAuthResolver(
        cmcc_host="https://ptest.cmccsim.com:9090",
        cmcc_send_auth_path="/trustedAuth/api/simAuth/sendAuth",
        cmcc_get_result_path="/trustedAuth/api/simAuth/getSimAuthResult",
        cmcc_ap_id="A0003",
        cmcc_app_id="A0003001",
        cmcc_private_key=_TEST_PRIVATE_KEY,
        cmcc_msisdn="19802025093",
        cmcc_template_id="DF20240419093514451c32",
        cmcc_poll_interval_sec=0.01,
        cmcc_poll_timeout_sec=1,
        cmcc_callback_timeout_sec=0,
    )

    async def _fake_cmcc_post(endpoint: str, _payload: dict[str, object]) -> dict[str, object]:
        if endpoint.endswith("/sendAuth"):
            return {"data": {"taskId": "task_timeout_1"}}
        return {"resultCode": "200", "resultDesc": "处理中"}

    monkeypatch.setattr(resolver, "_cmcc_post", _fake_cmcc_post)

    verdict = await resolver.verify(_make_request())
    assert verdict.decision == "deny"
    assert "timed out" in verdict.reason


@pytest.mark.asyncio
async def test_cmcc_verify_missing_task_id_returns_reason(monkeypatch) -> None:
    resolver = SimAuthResolver(
        cmcc_host="https://ptest.cmccsim.com:9090",
        cmcc_send_auth_path="/trustedAuth/api/simAuth/sendAuth",
        cmcc_get_result_path="/trustedAuth/api/simAuth/getSimAuthResult",
        cmcc_ap_id="A0003",
        cmcc_app_id="A0003001",
        cmcc_private_key=_TEST_PRIVATE_KEY,
        cmcc_msisdn="19802025093",
        cmcc_template_id="DF20240419093514451c32",
    )

    async def _fake_cmcc_post(_endpoint: str, _payload: dict[str, object]) -> dict[str, object]:
        return {"resultCode": "AC1000017", "resultDesc": "taskId:不能为空"}

    monkeypatch.setattr(resolver, "_cmcc_post", _fake_cmcc_post)

    verdict = await resolver.verify(_make_request())
    assert verdict.decision == "deny"
    assert "missing taskId" in verdict.reason
    assert "taskId:不能为空" in verdict.reason


@pytest.mark.asyncio
async def test_cmcc_verify_uses_callback_payload(monkeypatch) -> None:
    resolver = SimAuthResolver(
        cmcc_host="https://ptest.cmccsim.com:9090",
        cmcc_send_auth_path="/trustedAuth/api/simAuth/sendAuth",
        cmcc_get_result_path="/trustedAuth/api/simAuth/getSimAuthResult",
        cmcc_ap_id="A0003",
        cmcc_app_id="A0003001",
        cmcc_private_key=_TEST_PRIVATE_KEY,
        cmcc_msisdn="19802025093",
        cmcc_template_id="DF20240419093514451c32",
        cmcc_poll_interval_sec=0.01,
        cmcc_poll_timeout_sec=1,
        cmcc_callback_timeout_sec=1,
    )

    # Pretend callback listener is already active so callback futures are registered.
    monkeypatch.setattr(resolver, "_ensure_callback_server_started", lambda: None)
    resolver._callback_server = object()  # type: ignore[assignment]

    async def _fake_cmcc_post(endpoint: str, _payload: dict[str, object]) -> dict[str, object]:
        if endpoint.endswith("/sendAuth"):
            loop = asyncio.get_running_loop()
            loop.call_later(
                0.02,
                resolver._callback_bridge.ingest,
                {"taskId": "task_cb_1", "status": "0", "resultDesc": "Approved by callback"},
            )
            return {"data": {"taskId": "task_cb_1"}}
        return {"resultCode": "AC1000000", "resultDesc": "pending"}

    monkeypatch.setattr(resolver, "_cmcc_post", _fake_cmcc_post)

    verdict = await resolver.verify(_make_request())
    assert verdict.decision == "allow-once"
    assert "Approved by callback" in verdict.reason
