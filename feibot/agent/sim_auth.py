"""SIM-auth resolver for exec approval requests."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import httpx
from loguru import logger

from feibot.agent.exec_approval import ApprovalDecision, ExecApprovalManager, ExecApprovalRequest

try:
    from gmssl import sm2 as gmssl_sm2
except Exception:  # pragma: no cover - import depends on optional runtime package
    gmssl_sm2 = None


_ALLOW_HINTS = {
    "approved",
    "approve",
    "allow",
    "allow-once",
    "allow_once",
    "allowonce",
    "success",
    "succeeded",
    "ok",
    "passed",
    "pass",
    "true",
    "0",
}

_DENY_HINTS = {
    "denied",
    "deny",
    "rejected",
    "reject",
    "blocked",
    "block",
    "failed",
    "fail",
    "timeout",
    "expired",
    "false",
    "1",
    "2",
    "3",
    "-1",
}


@dataclass
class SimAuthDecision:
    """Decision returned by SIM-auth resolver."""

    decision: ApprovalDecision
    reason: str = ""


class _CallbackBridge:
    """Thread-safe callback bridge from HTTP handler to asyncio futures."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._buffered: dict[str, dict[str, Any]] = {}

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def register(self, task_id: str) -> asyncio.Future[dict[str, Any]]:
        loop = self._loop
        if loop is None:
            raise RuntimeError("Callback bridge loop is not bound.")
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        with self._lock:
            buffered = self._buffered.pop(task_id, None)
            if buffered is not None:
                loop.call_soon(self._safe_set_result, future, buffered)
            else:
                self._pending[task_id] = future
        return future

    def unregister(self, task_id: str, future: asyncio.Future[dict[str, Any]]) -> None:
        with self._lock:
            current = self._pending.get(task_id)
            if current is future:
                self._pending.pop(task_id, None)

    def ingest(self, payload: dict[str, Any]) -> None:
        task_id = self._extract_task_id(payload)
        if not task_id:
            logger.warning("SimAuth callback payload missing taskId: {}", payload)
            return
        loop = self._loop
        with self._lock:
            future = self._pending.pop(task_id, None)
            if future is None:
                self._buffered[task_id] = payload
                return
        if loop is not None:
            loop.call_soon_threadsafe(self._safe_set_result, future, payload)

    @staticmethod
    def _safe_set_result(
        future: asyncio.Future[dict[str, Any]],
        payload: dict[str, Any],
    ) -> None:
        if not future.done():
            future.set_result(payload)

    @staticmethod
    def _extract_task_id(payload: dict[str, Any]) -> str:
        task_id = str(payload.get("taskId") or payload.get("task_id") or "").strip()
        if task_id:
            return task_id
        data = payload.get("data")
        if isinstance(data, dict):
            return str(data.get("taskId") or data.get("task_id") or "").strip()
        return ""


class _CallbackHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server with callback bridge context."""

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        bridge: _CallbackBridge,
        callback_path: str,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.bridge = bridge
        self.callback_path = callback_path


class _CallbackRequestHandler(BaseHTTPRequestHandler):
    """Minimal JSON POST callback handler for CMCC SIM-auth responses."""

    server: _CallbackHTTPServer

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path != self.server.callback_path:
            self._send_json(404, {"error": "Not found"})
            return

        try:
            content_len = int(self.headers.get("Content-Length", "0") or "0")
        except Exception:
            content_len = 0
        raw = self.rfile.read(max(0, content_len))
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(payload, dict):
                payload = {"raw": payload}
        except Exception:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        task_id = str(payload.get("taskId") or payload.get("task_id") or "").strip()
        status = str(payload.get("status") or "").strip()
        callback_result_code = str(
            payload.get("callbackResultCode") or payload.get("callback_result_code") or ""
        ).strip()
        if not callback_result_code:
            data = payload.get("data")
            if isinstance(data, dict):
                callback_result_code = str(
                    data.get("callbackResultCode") or data.get("callback_result_code") or ""
                ).strip()
        logger.info(
            "CMCC SimAuth callback received: task_id={}, status={}, callback_result_code={}, from={}",
            task_id or "(missing)",
            status or "(missing)",
            callback_result_code or "(missing)",
            self.client_address[0] if isinstance(self.client_address, tuple) else "unknown",
        )
        self.server.bridge.ingest(payload)
        self._send_json(200, {"resultCode": "200", "resultDesc": "Received"})

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path != self.server.callback_path:
            self._send_json(404, {"error": "Not found"})
            return
        self._send_json(200, {"ok": True, "path": self.server.callback_path})

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        formatted = fmt % args if args else fmt
        logger.debug("SimAuth callback HTTP: {}", formatted)

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SimAuthResolver:
    """Resolve exec approvals through generic or CMCC SIM-auth endpoints."""

    def __init__(
        self,
        *,
        verify_url: str = "",
        api_key: str = "",
        timeout_sec: int = 90,
        extra_headers: dict[str, str] | None = None,
        cmcc_host: str = "",
        cmcc_send_auth_path: str = "",
        cmcc_get_result_path: str = "",
        cmcc_ap_id: str = "",
        cmcc_app_id: str = "",
        cmcc_private_key: str = "",
        cmcc_msisdn: str = "",
        cmcc_template_id: str = "",
        cmcc_callback_url: str = "",
        cmcc_callback_timeout_sec: int = 65,
        cmcc_poll_interval_sec: float = 2.0,
        cmcc_poll_timeout_sec: int = 65,
        cmcc_callback_listen_host: str = "",
        cmcc_callback_listen_port: int = 0,
        cmcc_callback_path: str = "/callback",
    ) -> None:
        self.verify_url = str(verify_url or "").strip()
        self.api_key = str(api_key or "").strip()
        self.timeout_sec = max(5, int(timeout_sec or 90))
        self.extra_headers = {
            str(k).strip(): str(v).strip()
            for k, v in (extra_headers or {}).items()
            if str(k).strip() and str(v).strip()
        }

        self.cmcc_host = str(cmcc_host or "").strip()
        self.cmcc_send_auth_path = str(cmcc_send_auth_path or "").strip()
        self.cmcc_get_result_path = str(cmcc_get_result_path or "").strip()
        self.cmcc_ap_id = str(cmcc_ap_id or "").strip()
        self.cmcc_app_id = str(cmcc_app_id or "").strip()
        self.cmcc_private_key = str(cmcc_private_key or "").strip()
        self.cmcc_msisdn = str(cmcc_msisdn or "").strip()
        self.cmcc_template_id = str(cmcc_template_id or "").strip()
        self.cmcc_callback_url = str(cmcc_callback_url or "").strip()
        self.cmcc_callback_timeout_sec = max(0, int(cmcc_callback_timeout_sec or 0))
        self.cmcc_poll_interval_sec = max(0.2, float(cmcc_poll_interval_sec or 2.0))
        self.cmcc_poll_timeout_sec = max(1, int(cmcc_poll_timeout_sec or 65))
        self.cmcc_callback_listen_host = str(cmcc_callback_listen_host or "").strip()
        self.cmcc_callback_listen_port = int(cmcc_callback_listen_port or 0)
        self.cmcc_callback_path = str(cmcc_callback_path or "/callback").strip() or "/callback"
        if not self.cmcc_callback_path.startswith("/"):
            self.cmcc_callback_path = f"/{self.cmcc_callback_path}"

        self._callback_bridge = _CallbackBridge()
        self._callback_server_lock = threading.Lock()
        self._callback_server: _CallbackHTTPServer | None = None
        self._callback_thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self.cmcc_enabled or bool(self.verify_url)

    @property
    def cmcc_enabled(self) -> bool:
        required = [
            self.cmcc_host,
            self.cmcc_send_auth_path,
            self.cmcc_get_result_path,
            self.cmcc_ap_id,
            self.cmcc_app_id,
            self.cmcc_private_key,
            self.cmcc_msisdn,
            self.cmcc_template_id,
        ]
        return all(bool(x) for x in required)

    async def verify(self, request: ExecApprovalRequest) -> SimAuthDecision:
        """Resolve approval via CMCC flow first, otherwise generic verifier URL."""
        if self.cmcc_enabled:
            return await self._verify_cmcc(request)
        return await self._verify_generic(request)

    async def _verify_generic(self, request: ExecApprovalRequest) -> SimAuthDecision:
        if not self.verify_url:
            return SimAuthDecision(decision="deny", reason="SimAuth verifier URL is not configured.")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)
        payload = self._build_payload(request)

        try:
            async with httpx.AsyncClient(timeout=float(self.timeout_sec)) as client:
                response = await client.post(self.verify_url, json=payload, headers=headers)
        except Exception as exc:
            return SimAuthDecision(decision="deny", reason=f"SimAuth request failed: {exc}")

        if response.status_code >= 400:
            body = (response.text or "").strip()
            if len(body) > 240:
                body = body[:240] + "..."
            reason = f"SimAuth HTTP {response.status_code}"
            if body:
                reason = f"{reason}: {body}"
            return SimAuthDecision(decision="deny", reason=reason)

        data: Any
        try:
            data = response.json()
        except Exception:
            data = (response.text or "").strip()

        decision, reason = self._parse_decision(data)
        if decision is None:
            detail = reason or self._preview_payload(data)
            message = "SimAuth response missing supported approval decision."
            if detail:
                message = f"{message} Response: {detail}"
            return SimAuthDecision(decision="deny", reason=message)
        return SimAuthDecision(decision=decision, reason=reason)

    async def _verify_cmcc(self, request: ExecApprovalRequest) -> SimAuthDecision:
        if gmssl_sm2 is None:
            return SimAuthDecision(
                decision="deny",
                reason="gmssl is required for CMCC SimAuth signing but is not installed.",
            )

        loop = asyncio.get_running_loop()
        self._callback_bridge.bind_loop(loop)
        self._ensure_callback_server_started()

        send_resp = await self._cmcc_send_auth()
        if send_resp is None:
            return SimAuthDecision(decision="deny", reason="CMCC sendAuth request failed.")

        task_id = self._extract_task_id(send_resp)
        if not task_id:
            reason = self._extract_reason(send_resp) or self._preview_payload(send_resp)
            return SimAuthDecision(
                decision="deny",
                reason=f"CMCC sendAuth missing taskId. Response: {reason}",
            )
        logger.info("CMCC SimAuth sendAuth accepted: task_id={}", task_id)

        callback_future: asyncio.Future[dict[str, Any]] | None = None
        callback_deadline: float | None = None
        if self._callback_server is not None and self.cmcc_callback_timeout_sec > 0:
            callback_future = self._callback_bridge.register(task_id)
            callback_deadline = time.monotonic() + float(self.cmcc_callback_timeout_sec)

        poll_deadline = time.monotonic() + float(self.cmcc_poll_timeout_sec)
        last_reason = ""

        try:
            while True:
                now = time.monotonic()

                if callback_future is not None and callback_future.done():
                    callback_payload = callback_future.result()
                    decision, reason = self._parse_decision(callback_payload)
                    if decision is not None:
                        logger.info(
                            "CMCC SimAuth resolved via callback: task_id={}, decision={}",
                            task_id,
                            decision,
                        )
                        return SimAuthDecision(decision=decision, reason=reason)
                    if reason:
                        last_reason = reason

                if now < poll_deadline:
                    result_payload = await self._cmcc_get_result(task_id)
                    if isinstance(result_payload, dict):
                        decision, reason = self._parse_decision(result_payload)
                        if decision is not None:
                            logger.info(
                                "CMCC SimAuth resolved via getResult polling: task_id={}, decision={}",
                                task_id,
                                decision,
                            )
                            return SimAuthDecision(decision=decision, reason=reason)
                        if reason:
                            last_reason = reason

                callback_waiting = callback_deadline is not None and now < callback_deadline
                poll_waiting = now < poll_deadline
                if not callback_waiting and not poll_waiting:
                    break

                await asyncio.sleep(self.cmcc_poll_interval_sec)
        finally:
            if callback_future is not None:
                self._callback_bridge.unregister(task_id, callback_future)

        timeout_reason = "CMCC SimAuth timed out waiting for callback/result."
        if last_reason:
            timeout_reason = f"{timeout_reason} Last response: {last_reason}"
        return SimAuthDecision(decision="deny", reason=timeout_reason)

    async def _cmcc_send_auth(self) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "msisdn": self.cmcc_msisdn,
            "templateId": self.cmcc_template_id,
            "templateParam": "TrustedAuth",
            "version": "1",
            "needCallbackSimResult": True,
        }
        if self.cmcc_callback_url:
            payload["callbackUrl"] = self.cmcc_callback_url
        return await self._cmcc_post(self.cmcc_send_auth_path, payload)

    async def _cmcc_get_result(self, task_id: str) -> dict[str, Any] | None:
        return await self._cmcc_post(self.cmcc_get_result_path, {"taskId": task_id})

    async def _cmcc_post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        signed_payload = self._cmcc_sign_request(payload)
        if signed_payload is None:
            return None
        url = self._join_url(self.cmcc_host, endpoint)
        try:
            async with httpx.AsyncClient(timeout=float(self.timeout_sec), verify=False) as client:
                response = await client.post(
                    url,
                    json=signed_payload,
                    headers={"Content-Type": "application/json"},
                )
        except Exception as exc:
            logger.warning("CMCC SimAuth request failed for {}: {}", endpoint, exc)
            return None

        data: Any
        try:
            data = response.json()
        except Exception:
            data = {"raw": (response.text or "").strip()}

        if response.status_code >= 400:
            logger.warning(
                "CMCC SimAuth HTTP {} on {}: {}",
                response.status_code,
                endpoint,
                self._preview_payload(data),
            )
        if isinstance(data, dict):
            return data
        return {"raw": str(data)}

    def _cmcc_sign_request(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        signed = dict(payload)
        if "transactionId" not in signed:
            signed["transactionId"] = self._generate_transaction_id()
        if "timestamp" not in signed:
            signed["timestamp"] = int(time.time() * 1000)
        if "apId" not in signed:
            signed["apId"] = self.cmcc_ap_id
        if "appId" not in signed:
            signed["appId"] = self.cmcc_app_id

        try:
            plain = json.dumps(signed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            signature = self._sign_sm2(plain, self.cmcc_private_key)
        except Exception as exc:
            logger.warning("CMCC SimAuth signing failed: {}", exc)
            return None

        signed["signature"] = signature
        return signed

    @staticmethod
    def _generate_transaction_id() -> str:
        now = datetime.now().strftime("%Y%m%d%H%M%S%f")[:-3]
        return f"{now}12345"

    def _ensure_callback_server_started(self) -> None:
        if not self.cmcc_callback_listen_host or self.cmcc_callback_listen_port <= 0:
            return
        with self._callback_server_lock:
            if self._callback_server is not None:
                return
            try:
                server = _CallbackHTTPServer(
                    (self.cmcc_callback_listen_host, self.cmcc_callback_listen_port),
                    _CallbackRequestHandler,
                    bridge=self._callback_bridge,
                    callback_path=self.cmcc_callback_path,
                )
                server.daemon_threads = True
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                self._callback_server = server
                self._callback_thread = thread
                logger.info(
                    "CMCC SimAuth callback listener started at http://{}:{}{}",
                    self.cmcc_callback_listen_host,
                    self.cmcc_callback_listen_port,
                    self.cmcc_callback_path,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to start CMCC SimAuth callback listener {}:{}{}: {}",
                    self.cmcc_callback_listen_host,
                    self.cmcc_callback_listen_port,
                    self.cmcc_callback_path,
                    exc,
                )

    def close(self) -> None:
        """Close callback HTTP listener if running."""
        with self._callback_server_lock:
            server = self._callback_server
            self._callback_server = None
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        self._callback_thread = None

    @staticmethod
    def _build_payload(request: ExecApprovalRequest) -> dict[str, Any]:
        return {
            "approval_id": request.id,
            "command": request.command,
            "working_dir": request.working_dir,
            "channel": request.channel,
            "chat_id": request.chat_id,
            "session_key": request.session_key,
            "requester_id": request.requester_id,
            "created_at": request.created_at.isoformat(),
            "expires_at": request.expires_at.isoformat(),
        }

    @staticmethod
    def _extract_task_id(payload: dict[str, Any]) -> str:
        data = payload.get("data")
        if isinstance(data, dict):
            task_id = str(data.get("taskId") or data.get("task_id") or "").strip()
            if task_id:
                return task_id
        return str(payload.get("taskId") or payload.get("task_id") or "").strip()

    def _parse_decision(self, payload: Any) -> tuple[ApprovalDecision | None, str]:
        if isinstance(payload, dict):
            if isinstance(payload.get("approved"), bool):
                reason = self._extract_reason(payload)
                return ("allow-once" if payload["approved"] else "deny"), reason

            cmcc_code_decision = self._parse_cmcc_result_code(payload)
            if cmcc_code_decision is not None:
                reason = self._extract_reason(payload)
                return cmcc_code_decision, reason

            decision_keys = ("decision", "status", "result", "auth_result")
            for key in decision_keys:
                decision = self._normalize_decision(payload.get(key))
                if decision:
                    reason = self._extract_reason(payload)
                    return decision, reason

            nested = payload.get("data")
            if isinstance(nested, dict):
                nested_decision, nested_reason = self._parse_decision(nested)
                if nested_decision:
                    return nested_decision, nested_reason

            return None, self._extract_reason(payload)

        if isinstance(payload, str):
            decision = self._normalize_decision(payload)
            return decision, ""

        return None, ""

    @staticmethod
    def _parse_cmcc_result_code(payload: dict[str, Any]) -> ApprovalDecision | None:
        code_keys = (
            "callbackResultCode",
            "callback_result_code",
            "simResultCode",
            "sim_result_code",
        )
        for key in code_keys:
            if key not in payload:
                continue
            return SimAuthResolver._normalize_cmcc_result_code(payload.get(key))
        return None

    @staticmethod
    def _normalize_cmcc_result_code(raw: Any) -> ApprovalDecision | None:
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            value = int(text)
        except Exception:
            return SimAuthResolver._normalize_decision(text)
        return "allow-once" if value == 0 else "deny"

    @staticmethod
    def _normalize_decision(raw: Any) -> ApprovalDecision | None:
        if isinstance(raw, bool):
            return "allow-once" if raw else "deny"
        value = str(raw or "").strip().lower()
        if not value:
            return None
        mapped = ExecApprovalManager.normalize_decision(value)
        if mapped:
            return mapped
        if value in _ALLOW_HINTS:
            return "allow-once"
        if value in _DENY_HINTS:
            return "deny"
        return None

    @staticmethod
    def _extract_reason(payload: dict[str, Any]) -> str:
        reason_keys = (
            "message",
            "reason",
            "callbackResultDesc",
            "callback_result_desc",
            "simResultDesc",
            "sim_result_desc",
            "resultDesc",
            "result_desc",
            "error",
            "error_message",
        )
        for key in reason_keys:
            value = payload.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return ""

    @staticmethod
    def _preview_payload(payload: Any) -> str:
        if isinstance(payload, str):
            text = payload.strip()
        else:
            try:
                text = json.dumps(payload, ensure_ascii=False)
            except Exception:
                text = str(payload)
        if len(text) > 240:
            text = text[:240] + "..."
        return text

    @staticmethod
    def _join_url(host: str, endpoint: str) -> str:
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            return endpoint
        return f"{host.rstrip('/')}/{endpoint.lstrip('/')}"

    def _sign_sm2(self, plain_text: str, private_key_b64: str) -> str:
        if gmssl_sm2 is None:  # pragma: no cover - guarded by caller
            raise RuntimeError("gmssl is not available")

        key_b64 = str(private_key_b64 or "").strip()
        if not key_b64:
            raise ValueError("Empty SM2 private key")
        missing_padding = len(key_b64) % 4
        if missing_padding:
            key_b64 += "=" * (4 - missing_padding)
        key_bytes = base64.b64decode(key_b64)

        if len(key_bytes) == 32:
            private_key_hex = binascii.hexlify(key_bytes).decode("utf-8")
        else:
            private_key_hex = self._extract_private_key_from_pkcs8(key_bytes)

        temp = gmssl_sm2.CryptSM2(private_key=private_key_hex, public_key="")
        if hasattr(temp, "_kg") and hasattr(temp, "ecc_table"):
            d_int = int(private_key_hex, 16)
            g_point = temp.ecc_table["g"]
            pub_point = temp._kg(d_int, g_point)
            public_key_hex = pub_point.replace("04", "", 1) if pub_point.startswith("04") else pub_point
            sm2_crypt = gmssl_sm2.CryptSM2(private_key=private_key_hex, public_key=public_key_hex)
        else:
            sm2_crypt = temp

        sig_hex = sm2_crypt.sign_with_sm3(plain_text.encode("utf-8"))
        der_sig = self._der_encode_signature(sig_hex[:64], sig_hex[64:])
        return base64.b64encode(der_sig).decode("utf-8")

    @staticmethod
    def _extract_private_key_from_pkcs8(pkcs8_bytes: bytes) -> str:
        marker = b"\x04\x20"
        start = 0
        while True:
            idx = pkcs8_bytes.find(marker, start)
            if idx == -1:
                break
            candidate = pkcs8_bytes[idx + 2: idx + 2 + 32]
            if len(candidate) == 32:
                return binascii.hexlify(candidate).decode("utf-8")
            start = idx + 1
        raise ValueError("Could not extract private key from PKCS#8.")

    @staticmethod
    def _der_encode_signature(r_hex: str, s_hex: str) -> bytes:
        r_int = int(r_hex, 16)
        s_int = int(s_hex, 16)

        def _encode_integer(value: int) -> bytes:
            hex_text = hex(value)[2:]
            if len(hex_text) % 2 != 0:
                hex_text = f"0{hex_text}"
            raw = binascii.unhexlify(hex_text)
            if raw[0] & 0x80:
                raw = b"\x00" + raw
            return b"\x02" + bytes([len(raw)]) + raw

        r_der = _encode_integer(r_int)
        s_der = _encode_integer(s_int)
        content = r_der + s_der
        return b"\x30" + bytes([len(content)]) + content
