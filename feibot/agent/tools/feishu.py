"""Feishu tools for file delivery."""

import json
import mimetypes
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import httpx

from feibot.agent.tools.base import Tool


class FeishuSendFileTool(Tool):
    """Upload a local file to Feishu and send it as a file message."""

    def __init__(
        self,
        app_id: str = "",
        app_secret: str = "",
        default_receive_id: str = "",
        default_receive_id_type: str = "open_id",
        allowed_dir: Path | None = None,
        base_url: str = "https://open.feishu.cn",
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self._default_receive_id_ctx: ContextVar[str] = ContextVar(
            "feishu_default_receive_id",
            default=default_receive_id,
        )
        self._default_receive_id_type_ctx: ContextVar[str] = ContextVar(
            "feishu_default_receive_id_type",
            default=default_receive_id_type,
        )
        self.allowed_dir = allowed_dir
        self.base_url = base_url.rstrip("/")

    @property
    def name(self) -> str:
        return "feishu_send_file"

    @property
    def description(self) -> str:
        return (
            "Send a local file to Feishu/Lark chat. The tool uploads the file, then sends "
            "a msg_type=file message to the target receiver. This DOES NOT insert files/images "
            "into Feishu DocX documents."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Local file path to upload and send.",
                },
                "receive_id": {
                    "type": "string",
                    "description": "Target receiver ID. If omitted, uses configured default.",
                },
                "receive_id_type": {
                    "type": "string",
                    "enum": ["open_id", "chat_id", "user_id", "union_id", "email"],
                    "description": "Type of receive_id.",
                },
                "note": {
                    "type": "string",
                    "description": "Optional text message sent before the file.",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Validate input only and do not call Feishu APIs.",
                },
            },
            "required": ["file_path"],
        }

    async def execute(
        self,
        file_path: str,
        receive_id: str | None = None,
        receive_id_type: str | None = None,
        note: str | None = None,
        dry_run: bool = False,
        **kwargs: Any,
    ) -> str:
        try:
            resolved = self._resolve_file_path(file_path)
        except Exception as e:
            return f"Error: {e}"

        target_id = (receive_id or self._default_receive_id_ctx.get() or "").strip()
        target_type = (receive_id_type or self._default_receive_id_type_ctx.get() or "open_id").strip()

        if not target_id:
            return "Error: Missing receive_id. Pass receive_id or configure channels.feishu.allow_from."

        if dry_run:
            return json.dumps(
                {
                    "ok": True,
                    "mode": "dry-run",
                    "file_path": str(resolved),
                    "size_bytes": resolved.stat().st_size,
                    "receive_id": target_id,
                    "receive_id_type": target_type,
                    "restricted_dir": str(self.allowed_dir) if self.allowed_dir else None,
                },
                ensure_ascii=False,
                indent=2,
            )

        if not self.app_id or not self.app_secret:
            return "Error: Feishu credentials not configured (channels.feishu.app_id/app_secret)."

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                token = await self._get_tenant_token(client)
                if note:
                    await self._send_text(client, token, target_id, target_type, note)
                file_key = await self._upload_file(client, token, resolved)
                message_id = await self._send_file(client, token, target_id, target_type, file_key)

            return json.dumps(
                {
                    "ok": True,
                    "file_path": str(resolved),
                    "receive_id": target_id,
                    "receive_id_type": target_type,
                    "file_key": file_key,
                    "message_id": message_id,
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as e:
            return f"Error sending file via Feishu: {e}"

    def _resolve_file_path(self, file_path: str) -> Path:
        path = Path(file_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        if not path.is_file():
            raise ValueError(f"Not a file: {file_path}")

        if self.allowed_dir:
            base = self.allowed_dir.expanduser().resolve()
            try:
                path.relative_to(base)
            except ValueError as e:
                raise PermissionError(f"Path {path} is outside allowed directory {base}") from e

        return path

    async def _get_tenant_token(self, client: httpx.AsyncClient) -> str:
        url = f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal"
        resp = await client.post(url, json={"app_id": self.app_id, "app_secret": self.app_secret})
        data = self._safe_json(resp)
        if data.get("code") != 0 or not data.get("tenant_access_token"):
            raise RuntimeError(self._format_api_error("token request failed", resp, data))
        return data["tenant_access_token"]

    async def _upload_file(self, client: httpx.AsyncClient, token: str, file_path: Path) -> str:
        url = f"{self.base_url}/open-apis/im/v1/files"
        mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        with file_path.open("rb") as fh:
            files = {"file": (file_path.name, fh, mime)}
            data = {"file_type": "stream", "file_name": file_path.name}
            resp = await client.post(
                url,
                data=data,
                files=files,
                headers={"Authorization": f"Bearer {token}"},
            )
        payload = self._safe_json(resp)
        if payload.get("code") != 0:
            raise RuntimeError(self._format_api_error("upload failed", resp, payload))
        file_key = (payload.get("data") or {}).get("file_key")
        if not file_key:
            raise RuntimeError(f"upload succeeded but file_key missing: {payload}")
        return file_key

    async def _send_file(
        self,
        client: httpx.AsyncClient,
        token: str,
        receive_id: str,
        receive_id_type: str,
        file_key: str,
    ) -> str | None:
        url = f"{self.base_url}/open-apis/im/v1/messages"
        params = {"receive_id_type": receive_id_type}
        payload = {
            "receive_id": receive_id,
            "msg_type": "file",
            "content": json.dumps({"file_key": file_key}, ensure_ascii=False),
        }
        resp = await client.post(
            url,
            params=params,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        data = self._safe_json(resp)
        if data.get("code") != 0:
            raise RuntimeError(self._format_api_error("send file message failed", resp, data))
        return (data.get("data") or {}).get("message_id")

    async def _send_text(
        self,
        client: httpx.AsyncClient,
        token: str,
        receive_id: str,
        receive_id_type: str,
        text: str,
    ) -> None:
        url = f"{self.base_url}/open-apis/im/v1/messages"
        params = {"receive_id_type": receive_id_type}
        payload = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        resp = await client.post(
            url,
            params=params,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        data = self._safe_json(resp)
        if data.get("code") != 0:
            raise RuntimeError(self._format_api_error("send text message failed", resp, data))

    def _safe_json(self, resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
            return data if isinstance(data, dict) else {"raw": data}
        except Exception:
            text = (resp.text or "").strip()
            return {"raw_text": text}

    def _format_api_error(self, stage: str, resp: httpx.Response, payload: dict[str, Any]) -> str:
        code = payload.get("code", "")
        msg = payload.get("msg", "")
        details = []
        if code == 99991672:
            details.append(
                "missing app permission scopes: im:resource:upload or im:resource. "
                "Open Feishu Open Platform and grant one of these scopes."
            )
        err_obj = payload.get("error")
        if isinstance(err_obj, dict):
            log_id = err_obj.get("log_id")
            if log_id:
                details.append(f"log_id={log_id}")
        detail_text = f" ({'; '.join(details)})" if details else ""
        return f"{stage}: http={resp.status_code}, code={code}, msg={msg}{detail_text}"

    def set_context(self, chat_id: str) -> None:
        """Update the default receive_id and receive_id_type based on current chat context.
        
        Group chats (oc_*) use chat_id type.
        Direct messages (ou_*) use open_id type.
        Uses ContextVar for thread-safe per-request context.
        """
        if not chat_id:
            return
        self._default_receive_id_ctx.set(chat_id)
        # Group chats start with "oc_", direct messages start with "ou_"
        self._default_receive_id_type_ctx.set("chat_id" if chat_id.startswith("oc_") else "open_id")
