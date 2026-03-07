"""Feishu tools for messaging, DocX, and Bitable operations."""

import json
import mimetypes
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from feibot.agent.tools.base import Tool

try:
    import lark_oapi as lark
    import lark_oapi.api.bitable.v1 as lark_bitable
    import lark_oapi.api.docx.v1 as lark_docx
    import lark_oapi.api.wiki.v2 as lark_wiki_v2

    FEISHU_SDK_AVAILABLE = True
except ImportError:
    lark = None
    lark_bitable = None
    lark_docx = None
    lark_wiki_v2 = None
    FEISHU_SDK_AVAILABLE = False

try:
    import lark_oapi.api.drive.v1 as lark_drive

    FEISHU_DRIVE_SDK_AVAILABLE = True
except ImportError:
    lark_drive = None
    FEISHU_DRIVE_SDK_AVAILABLE = False

try:
    import lark_oapi.api.application.v6 as lark_application_v6

    FEISHU_APPLICATION_SDK_AVAILABLE = True
except ImportError:
    lark_application_v6 = None
    FEISHU_APPLICATION_SDK_AVAILABLE = False


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
        self.default_receive_id = default_receive_id
        self.default_receive_id_type = default_receive_id_type
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

        target_id = (receive_id or self.default_receive_id or "").strip()
        target_type = (receive_id_type or self.default_receive_id_type or "open_id").strip()

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
                raise PermissionError(
                    f"Path {path} is outside allowed directory {base}"
                ) from e

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


class _FeishuSdkToolBase(Tool):
    """Shared helpers for Feishu SDK-based tools (DocX / Bitable)."""

    def __init__(self, app_id: str = "", app_secret: str = ""):
        self.app_id = app_id
        self.app_secret = app_secret

    def _json(self, data: dict[str, Any]) -> str:
        return json.dumps(data, ensure_ascii=False, indent=2)

    def _sdk_unavailable_error(self) -> str:
        return "Error: Feishu SDK not installed. Run: pip install lark-oapi"

    def _missing_credentials_error(self) -> str:
        return "Error: Feishu credentials not configured (channels.feishu.app_id/app_secret)."

    def _create_client(self):
        if not FEISHU_SDK_AVAILABLE or lark is None:
            raise RuntimeError(self._sdk_unavailable_error())
        if not self.app_id or not self.app_secret:
            raise RuntimeError(self._missing_credentials_error())
        return (
            lark.Client.builder()
            .app_id(self.app_id)
            .app_secret(self.app_secret)
            .build()
        )

    def _require_success(self, response: Any, stage: str) -> None:
        ok = False
        if hasattr(response, "success") and callable(response.success):
            try:
                ok = bool(response.success())
            except Exception:
                ok = False
        else:
            ok = getattr(response, "code", None) == 0
        if ok:
            return
        code = getattr(response, "code", None)
        msg = getattr(response, "msg", None)
        log_id = None
        if hasattr(response, "get_log_id") and callable(response.get_log_id):
            try:
                log_id = response.get_log_id()
            except Exception:
                log_id = None
        tail = f", log_id={log_id}" if log_id else ""
        raise RuntimeError(f"{stage} failed: code={code}, msg={msg}{tail}")

    def _to_jsonable(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(k): self._to_jsonable(v) for k, v in value.items() if v is not None}
        if isinstance(value, (list, tuple, set)):
            return [self._to_jsonable(v) for v in value if v is not None]
        if hasattr(value, "__dict__"):
            out: dict[str, Any] = {}
            for key, v in vars(value).items():
                if key.startswith("_") or v is None:
                    continue
                out[key] = self._to_jsonable(v)
            return out
        return str(value)


_DOCX_URL_RE = re.compile(r"/docx/([A-Za-z0-9_-]+)")
_WIKI_URL_RE = re.compile(r"/wiki/([A-Za-z0-9_-]+)")
# Keep empty by default: DocX tables should be preserved, not silently dropped.
_DOCX_UNSUPPORTED_CREATE_TYPES: set[int] = set()
_DOCX_WRITE_CHUNK_CHARS_DEFAULT = 3500
_DOCX_WRITE_CHUNK_CHARS_MIN = 500
_DOCX_WRITE_CHUNK_CHARS_MAX = 12000
_DOCX_WRITE_MAX_RETRIES = 3
_DOCX_WRITE_RETRY_BASE_SECONDS = 0.4
_DOCX_WRITE_AUTO_CHUNK_THRESHOLD_DEFAULT = 6000
_DOCX_MD_TABLE_ROW_RE = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
_DOCX_MD_TABLE_SEPARATOR_RE = re.compile(r"^[ \t]*\|(?:\s*:?-{3,}:?\s*\|)+[ \t]*$")
_FEISHU_WIKI_ACCESS_HINT = (
    "Wiki access hint: add the bot (or a group containing the bot) to the knowledge base space "
    "with edit/admin permissions, then re-authorize the app if scopes changed."
)
_FEISHU_PERM_TOKEN_TYPES = (
    "doc",
    "docx",
    "sheet",
    "bitable",
    "folder",
    "file",
    "wiki",
    "mindnote",
    "minutes",
    "slides",
)
_FEISHU_PERM_MEMBER_TYPES = (
    "email",
    "openid",
    "userid",
    "unionid",
    "openchat",
    "opendepartmentid",
    "groupid",
    "wikispaceid",
)
_FEISHU_PERM_VALUES = ("view", "edit", "full_access")
_FEISHU_DRIVE_FILE_TYPES = (
    "doc",
    "docx",
    "sheet",
    "bitable",
    "folder",
    "file",
    "mindnote",
    "slides",
    "shortcut",
)
_BITABLE_FIELD_TYPE_NAMES = {
    1: "Text",
    2: "Number",
    3: "SingleSelect",
    4: "MultiSelect",
    5: "DateTime",
    7: "Checkbox",
    11: "User",
    13: "Phone",
    15: "URL",
    17: "Attachment",
    18: "SingleLink",
    19: "Lookup",
    20: "Formula",
    21: "DuplexLink",
    22: "Location",
    23: "GroupChat",
    1001: "CreatedTime",
    1002: "ModifiedTime",
    1003: "CreatedUser",
    1004: "ModifiedUser",
    1005: "AutoNumber",
}
_DOCX_BLOCK_TYPE_NAMES = {
    1: "Page",
    2: "Text",
    3: "Heading1",
    4: "Heading2",
    5: "Heading3",
    6: "Heading4",
    7: "Heading5",
    8: "Heading6",
    9: "Heading7",
    10: "Heading8",
    11: "Heading9",
    12: "Bullet",
    13: "Ordered",
    14: "Code",
    15: "Quote",
    17: "Todo",
    18: "Bitable",
    21: "Diagram",
    22: "Divider",
    23: "File",
    27: "Image",
    30: "Sheet",
    31: "Table",
    32: "TableCell",
}


class FeishuDocTool(_FeishuSdkToolBase):
    """Feishu DocX operations using the official SDK."""

    def __init__(
        self,
        app_id: str = "",
        app_secret: str = "",
        owner_open_id: str = "",
        wiki_space_id: str = "",
        wiki_parent_node_token: str = "",
        auto_chunk_threshold_chars: int = _DOCX_WRITE_AUTO_CHUNK_THRESHOLD_DEFAULT,
    ):
        super().__init__(app_id=app_id, app_secret=app_secret)
        self.owner_open_id = (owner_open_id or "").strip()
        self.wiki_space_id = (wiki_space_id or "").strip()
        self.wiki_parent_node_token = (wiki_parent_node_token or "").strip()
        self.auto_chunk_threshold_chars = max(0, int(auto_chunk_threshold_chars or 0))

    @property
    def name(self) -> str:
        return "feishu_doc"

    @property
    def description(self) -> str:
        return (
            "Operate Feishu DocX documents. Actions: read, list_blocks, get_block, update_block, "
            "delete_block, create, append, write, write_safe, insert_image. Use doc_token (or a docx URL). "
            "Use action=insert_image to place a local image file (for example a cached Feishu image path) "
            "into a DocX document; do not use feishu_send_file for DocX insertion. "
            "If wiki defaults are configured, create will place docs into the enterprise knowledge base."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "oneOf": [
                self._doc_action_schema("read", require_doc_ref=True),
                self._doc_action_schema(
                    "list_blocks",
                    require_doc_ref=True,
                    extra_properties={
                        "page_size": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 500,
                            "description": "Page size for list_blocks (default 200).",
                        },
                        "page_token": {
                            "type": "string",
                            "description": "Pagination token for list_blocks.",
                        },
                    },
                ),
                self._doc_action_schema(
                    "get_block",
                    require_doc_ref=True,
                    extra_properties={
                        "block_id": {
                            "type": "string",
                            "description": "DocX block_id for action=get_block.",
                        },
                    },
                    required=["block_id"],
                ),
                self._doc_action_schema(
                    "update_block",
                    require_doc_ref=True,
                    extra_properties={
                        "block_id": {
                            "type": "string",
                            "description": "DocX block_id for action=update_block.",
                        },
                        "content": {
                            "type": "string",
                            "description": "Plain text content for action=update_block.",
                        },
                    },
                    required=["block_id", "content"],
                ),
                self._doc_action_schema(
                    "delete_block",
                    require_doc_ref=True,
                    extra_properties={
                        "block_id": {
                            "type": "string",
                            "description": "DocX block_id for action=delete_block (not the document token).",
                        },
                    },
                    required=["block_id"],
                ),
                self._doc_action_schema(
                    "create",
                    extra_properties={
                        "title": {
                            "type": "string",
                            "description": "Title for action=create.",
                        },
                        "folder_token": {
                            "type": "string",
                            "description": "Optional folder token for action=create (do not use wiki node token here).",
                        },
                        "wiki_space_id": {
                            "type": "string",
                            "description": "Optional wiki knowledge space ID for action=create (overrides configured default).",
                        },
                        "wiki_parent_node_token": {
                            "type": "string",
                            "description": "Optional wiki parent node token for action=create (overrides configured default).",
                        },
                    },
                    required=["title"],
                ),
                self._doc_action_schema(
                    "append",
                    require_doc_ref=True,
                    extra_properties={
                        "content": {
                            "type": "string",
                            "description": "Markdown content for action=append.",
                        },
                    },
                    required=["content"],
                ),
                self._doc_action_schema(
                    "write",
                    require_doc_ref=True,
                    extra_properties={
                        "content": {
                            "type": "string",
                            "description": "Markdown content for action=write (replaces document content).",
                        },
                    },
                    required=["content"],
                ),
                self._doc_action_schema(
                    "write_safe",
                    require_doc_ref=True,
                    extra_properties={
                        "content": {
                            "type": "string",
                            "description": (
                                "Markdown content for action=write_safe (replaces document content). "
                                "The tool writes in internal chunks with retry to reduce large-write failures."
                            ),
                        },
                        "chunk_chars": {
                            "type": "integer",
                            "minimum": _DOCX_WRITE_CHUNK_CHARS_MIN,
                            "maximum": _DOCX_WRITE_CHUNK_CHARS_MAX,
                            "description": (
                                "Approximate chunk size for action=write_safe (default 3500). "
                                "Lower values are safer; higher values are faster."
                            ),
                        },
                    },
                    required=["content"],
                ),
                self._doc_action_schema(
                    "insert_image",
                    require_doc_ref=True,
                    extra_properties={
                        "image_path": {
                            "type": "string",
                            "description": "Local image file path for action=insert_image.",
                        },
                        "image_width": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Optional image width for action=insert_image.",
                        },
                        "image_height": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Optional image height for action=insert_image.",
                        },
                        "image_scale": {
                            "type": "number",
                            "exclusiveMinimum": 0,
                            "description": "Optional image scale for action=insert_image (e.g. 0.5, 1.0).",
                        },
                    },
                    required=["image_path"],
                ),
            ],
        }

    def _doc_action_schema(
        self,
        action: str,
        *,
        require_doc_ref: bool = False,
        extra_properties: dict[str, Any] | None = None,
        required: list[str] | None = None,
    ) -> dict[str, Any]:
        props: dict[str, Any] = {
            "action": {
                "type": "string",
                "enum": [action],
                "description": "DocX operation.",
            }
        }
        if require_doc_ref:
            props.update(
                {
                    "doc_token": {
                        "type": "string",
                        "description": "Doc token (document_id) from a /docx/<token> URL.",
                    },
                    "url": {
                        "type": "string",
                        "description": "Optional DocX URL. If provided, doc_token can be omitted.",
                    },
                }
            )
        if extra_properties:
            props.update(extra_properties)

        schema: dict[str, Any] = {
            "type": "object",
            "properties": props,
            "required": ["action", *(required or [])],
            "additionalProperties": False,
        }
        if require_doc_ref:
            schema["anyOf"] = [{"required": ["doc_token"]}, {"required": ["url"]}]
        return schema

    async def execute(
        self,
        action: str,
        doc_token: str | None = None,
        url: str | None = None,
        title: str | None = None,
        block_id: str | None = None,
        folder_token: str | None = None,
        wiki_space_id: str | None = None,
        wiki_parent_node_token: str | None = None,
        content: str | None = None,
        image_path: str | None = None,
        image_width: int | None = None,
        image_height: int | None = None,
        image_scale: float | None = None,
        chunk_chars: int | None = None,
        page_size: int = 200,
        page_token: str | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            client = self._create_client()

            if action == "create":
                if not (title or "").strip():
                    return "Error: title is required for feishu_doc action=create"
                return self._create_doc(
                    client,
                    title=title.strip(),
                    folder_token=folder_token,
                    wiki_space_id=wiki_space_id,
                    wiki_parent_node_token=wiki_parent_node_token,
                )

            resolved_doc_token = self._resolve_doc_token(doc_token=doc_token, url=url)

            if action == "list_blocks":
                return self._list_blocks(
                    client,
                    resolved_doc_token,
                    page_size=max(1, min(page_size or 200, 500)),
                    page_token=page_token,
                )
            if action == "read":
                return self._read_doc(client, resolved_doc_token)
            if action == "get_block":
                if not (block_id or "").strip():
                    return "Error: block_id is required for feishu_doc action=get_block"
                return self._get_block(client, resolved_doc_token, block_id.strip())
            if action == "update_block":
                if not (block_id or "").strip():
                    return "Error: block_id is required for feishu_doc action=update_block"
                if content is None:
                    return "Error: content is required for feishu_doc action=update_block"
                return self._update_block_text(
                    client,
                    resolved_doc_token,
                    block_id=block_id.strip(),
                    content=content,
                )
            if action == "delete_block":
                if not (block_id or "").strip():
                    return "Error: block_id is required for feishu_doc action=delete_block"
                return self._delete_block(client, resolved_doc_token, block_id.strip())
            if action in {"append", "write", "write_safe"}:
                if content is None:
                    return f"Error: content is required for feishu_doc action={action}"
                auto_force_chunked = self._should_auto_force_chunked(action=action, content=content)
                return self._write_or_append_doc(
                    client,
                    resolved_doc_token,
                    markdown=content,
                    replace=(action in {"write", "write_safe"}),
                    force_chunked=(action == "write_safe") or auto_force_chunked,
                    chunk_chars=chunk_chars,
                )
            if action == "insert_image":
                if not (image_path or "").strip():
                    return "Error: image_path is required for feishu_doc action=insert_image"
                return self._insert_image_into_doc(
                    client,
                    resolved_doc_token,
                    image_path=image_path.strip(),
                    width=image_width,
                    height=image_height,
                    scale=image_scale,
                )
            return f"Error: Unsupported feishu_doc action={action}"
        except Exception as e:
            return f"Error: {e}"

    def _resolve_doc_token(self, doc_token: str | None, url: str | None) -> str:
        if doc_token and doc_token.strip():
            return doc_token.strip()
        if not url:
            raise ValueError("doc_token (or url) is required")
        match = _DOCX_URL_RE.search(url)
        if not match:
            raise ValueError(f"Could not extract doc token from url: {url}")
        return match.group(1)

    def _create_doc(
        self,
        client: Any,
        title: str,
        folder_token: str | None,
        wiki_space_id: str | None = None,
        wiki_parent_node_token: str | None = None,
    ) -> str:
        target_wiki_space_id = (wiki_space_id or self.wiki_space_id or "").strip()
        target_wiki_parent = (wiki_parent_node_token or self.wiki_parent_node_token or "").strip()

        created_via_wiki = False
        wiki_node: dict[str, Any] | None = None
        doc = None
        doc_id: str | None = None

        # Keep folder_token behavior for explicit folder placement. Otherwise prefer wiki placement
        # when a default wiki space or parent wiki node is configured.
        if not folder_token and (target_wiki_space_id or target_wiki_parent):
            doc_id, wiki_node = self._create_doc_via_wiki(
                client,
                title=title,
                wiki_space_id=target_wiki_space_id or None,
                wiki_parent_node_token=target_wiki_parent or None,
            )
            created_via_wiki = True
            doc = self._doc_stub(document_id=doc_id, title=title)
        else:
            body_builder = lark_docx.CreateDocumentRequestBody.builder().title(title)
            if folder_token:
                body_builder = body_builder.folder_token(folder_token)
            req = lark_docx.CreateDocumentRequest.builder().request_body(body_builder.build()).build()
            resp = client.docx.v1.document.create(req)
            self._require_success(resp, "Create document")
            doc = getattr(getattr(resp, "data", None), "document", None)
            doc_id = getattr(doc, "document_id", None) if doc else None

        owner_admin_grant = None
        if doc_id and self.owner_open_id:
            owner_admin_grant = self._grant_doc_admin_permission_best_effort(
                client=client,
                doc_token=str(doc_id),
                owner_open_id=self.owner_open_id,
            )
        return self._json(
            {
                "ok": True,
                "action": "create",
                "document": self._doc_summary(doc),
                "url": f"https://feishu.cn/docx/{doc_id}" if doc_id else None,
                "created_via": "wiki" if created_via_wiki else "docx",
                "wiki_node": wiki_node,
                "owner_admin_grant": owner_admin_grant,
            }
        )

    def _create_doc_via_wiki(
        self,
        client: Any,
        title: str,
        wiki_space_id: str | None,
        wiki_parent_node_token: str | None,
    ) -> tuple[str, dict[str, Any]]:
        if lark_wiki_v2 is None or not hasattr(client, "wiki") or client.wiki is None:
            raise RuntimeError("Wiki SDK not available in current lark-oapi installation")
        resolved_space_id = (wiki_space_id or "").strip()
        if not resolved_space_id:
            if not wiki_parent_node_token:
                raise RuntimeError("wiki_space_id or wiki_parent_node_token is required for wiki doc creation")
            resolved_space_id = self._resolve_wiki_space_id_from_node(client, wiki_parent_node_token)

        node_builder = (
            lark_wiki_v2.Node.builder()
            .obj_type("docx")
            .node_type("origin")
            .title(title)
        )
        if wiki_parent_node_token:
            node_builder = node_builder.parent_node_token(wiki_parent_node_token)
        req = (
            lark_wiki_v2.CreateSpaceNodeRequest.builder()
            .space_id(resolved_space_id)
            .request_body(node_builder.build())
            .build()
        )
        resp = client.wiki.v2.space_node.create(req)
        self._require_success(resp, "Create wiki doc node")
        node = getattr(getattr(resp, "data", None), "node", None)
        doc_token = (getattr(node, "obj_token", None) or "").strip()
        if not doc_token:
            raise RuntimeError("Wiki create succeeded but obj_token(docx token) missing")
        node_token = getattr(node, "node_token", None)
        wiki_node = {
            "space_id": str(getattr(node, "space_id", resolved_space_id) or resolved_space_id),
            "node_token": node_token,
            "parent_node_token": getattr(node, "parent_node_token", None) or wiki_parent_node_token,
            "obj_type": getattr(node, "obj_type", None) or "docx",
            "obj_token": doc_token,
            "title": getattr(node, "title", None) or title,
            "wiki_url": f"https://feishu.cn/wiki/{node_token}" if node_token else None,
        }
        return doc_token, wiki_node

    def _resolve_wiki_space_id_from_node(self, client: Any, wiki_node_token: str) -> str:
        if lark_wiki_v2 is None or not hasattr(client, "wiki") or client.wiki is None:
            raise RuntimeError("Wiki SDK not available in current lark-oapi installation")
        req = (
            lark_wiki_v2.GetNodeSpaceRequest.builder()
            .token(wiki_node_token)
            .build()
        )
        resp = client.wiki.v2.space.get_node(req)
        self._require_success(resp, "Resolve wiki space by node")
        node = getattr(getattr(resp, "data", None), "node", None)
        space_id = getattr(node, "space_id", None)
        if space_id in (None, ""):
            raise RuntimeError("Wiki node lookup succeeded but space_id missing")
        return str(space_id)

    def _doc_stub(self, document_id: str, title: str) -> Any:
        class _DocStub:
            pass

        stub = _DocStub()
        stub.document_id = document_id
        stub.title = title
        stub.revision_id = None
        return stub

    def _grant_doc_admin_permission_best_effort(
        self,
        client: Any,
        doc_token: str,
        owner_open_id: str,
    ) -> dict[str, Any]:
        info: dict[str, Any] = {
            "attempted": True,
            "member_type": "openid",
            "member_id": owner_open_id,
            "perm": "full_access",
            "token": doc_token,
        }

        if not FEISHU_DRIVE_SDK_AVAILABLE or lark_drive is None:
            info["status"] = "skipped"
            info["reason"] = "drive sdk not available"
            return info

        body = (
            lark_drive.BaseMember.builder()
            .member_type("openid")
            .member_id(owner_open_id)
            .perm("full_access")
            .build()
        )

        errors: list[str] = []
        for doc_type in ("docx", "doc"):
            try:
                req = (
                    lark_drive.CreatePermissionMemberRequest.builder()
                    .type(doc_type)
                    .need_notification(False)
                    .token(doc_token)
                    .request_body(body)
                    .build()
                )
                resp = client.drive.v1.permission_member.create(req)
                self._require_success(resp, "Grant DocX admin permission")
                info["status"] = "granted"
                info["doc_type"] = doc_type
                member = getattr(getattr(resp, "data", None), "member", None)
                if member is not None:
                    info["member"] = self._to_jsonable(member)
                return info
            except Exception as e:
                errors.append(f"{doc_type}: {e}")

        info["status"] = "failed"
        info["error"] = " | ".join(errors) if errors else "unknown error"
        return info

    def _insert_image_into_doc(
        self,
        client: Any,
        doc_token: str,
        image_path: str,
        width: int | None = None,
        height: int | None = None,
        scale: float | None = None,
    ) -> str:
        local_path = self._resolve_local_image_path(image_path)
        image_block_id = self._create_docx_image_placeholder_block(client, doc_token)
        file_token = self._upload_local_image_to_docx_image_slot(client, image_block_id, local_path)
        self._replace_docx_image_block(
            client,
            doc_token=doc_token,
            block_id=image_block_id,
            file_token=file_token,
            width=width,
            height=height,
            scale=scale,
        )
        return self._json(
            {
                "ok": True,
                "action": "insert_image",
                "doc_token": doc_token,
                "image_path": str(local_path),
                "image_block_id": image_block_id,
                "file_token": file_token,
                "url": f"https://feishu.cn/docx/{doc_token}",
            }
        )

    def _resolve_local_image_path(self, image_path: str) -> Path:
        path = Path(image_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")
        if not path.is_file():
            raise ValueError(f"Not a file: {image_path}")
        mime = mimetypes.guess_type(path.name)[0] or ""
        if not mime.startswith("image/"):
            raise ValueError(f"Not an image file: {image_path}")
        return path

    def _create_docx_image_placeholder_block(self, client: Any, doc_token: str) -> str:
        # Prefer markdown conversion because it yields a server-accepted placeholder image block shape.
        placeholder_url = "https://example.com/placeholder.png"
        markdown = f"![image]({placeholder_url})"
        converted_blocks, first_level_ids = self._convert_markdown_to_blocks(client, markdown)
        ordered_blocks = self._sort_blocks_by_first_level(converted_blocks, first_level_ids)
        cleaned_blocks, _ = self._clean_blocks_for_insert(ordered_blocks)
        if not cleaned_blocks:
            raise RuntimeError("Could not create image placeholder block from markdown conversion")
        inserted = self._insert_blocks(client, doc_token, cleaned_blocks)
        for block in inserted:
            if int(getattr(block, "block_type", 0) or 0) == 27 and getattr(block, "block_id", None):
                return str(getattr(block, "block_id"))
        raise RuntimeError("Inserted blocks did not include an image placeholder block")

    def _upload_local_image_to_docx_image_slot(self, client: Any, block_id: str, image_path: Path) -> str:
        if not FEISHU_DRIVE_SDK_AVAILABLE or lark_drive is None:
            raise RuntimeError("Feishu Drive SDK not available; cannot upload image media")

        with image_path.open("rb") as fh:
            body = (
                lark_drive.UploadAllMediaRequestBody.builder()
                .file_name(image_path.name)
                .parent_type("docx_image")
                .parent_node(block_id)
                .size(image_path.stat().st_size)
                .file(fh)
                .build()
            )
            req = lark_drive.UploadAllMediaRequest.builder().request_body(body).build()
            resp = client.drive.v1.media.upload_all(req)
        self._require_success(resp, "Upload DocX image media")
        file_token = getattr(getattr(resp, "data", None), "file_token", None)
        if not file_token:
            raise RuntimeError("Image upload succeeded but file_token missing")
        return str(file_token)

    def _replace_docx_image_block(
        self,
        client: Any,
        doc_token: str,
        block_id: str,
        file_token: str,
        width: int | None = None,
        height: int | None = None,
        scale: float | None = None,
    ) -> None:
        replace_builder = lark_docx.ReplaceImageRequest.builder().token(file_token)
        if width and width > 0:
            replace_builder = replace_builder.width(int(width))
        if height and height > 0:
            replace_builder = replace_builder.height(int(height))
        if scale and scale > 0:
            replace_builder = replace_builder.scale(float(scale))
        body = lark_docx.UpdateBlockRequest.builder().replace_image(replace_builder.build()).build()
        req = (
            lark_docx.PatchDocumentBlockRequest.builder()
            .document_id(doc_token)
            .block_id(block_id)
            .client_token(str(uuid.uuid4()))
            .request_body(body)
            .build()
        )
        resp = client.docx.v1.document_block.patch(req)
        self._require_success(resp, "Replace DocX image block")

    def _read_doc(self, client: Any, doc_token: str) -> str:
        get_req = lark_docx.GetDocumentRequest.builder().document_id(doc_token).build()
        get_resp = client.docx.v1.document.get(get_req)
        self._require_success(get_resp, "Get document")
        doc = getattr(getattr(get_resp, "data", None), "document", None)

        blocks = self._list_all_document_blocks(client, doc_token)
        block_counts: dict[str, int] = {}
        lines: list[str] = []
        for block in blocks:
            block_type = int(getattr(block, "block_type", 0) or 0)
            type_name = _DOCX_BLOCK_TYPE_NAMES.get(block_type, f"type_{block_type}")
            block_counts[type_name] = block_counts.get(type_name, 0) + 1
            text = self._extract_block_text(block)
            if text:
                lines.append(text)
            elif block_type == 22:
                lines.append("---")
            elif block_type in {27, 31, 18, 23, 30}:
                lines.append(f"[{type_name}]")

        plain_text = "\n".join(line for line in lines if line).strip()
        return self._json(
            {
                "ok": True,
                "action": "read",
                "doc_token": doc_token,
                "document": self._doc_summary(doc),
                "block_count": len(blocks),
                "block_types": block_counts,
                "text": plain_text,
            }
        )

    def _list_blocks(self, client: Any, doc_token: str, page_size: int, page_token: str | None) -> str:
        items, next_token, has_more = self._list_document_blocks_page(
            client, doc_token, page_size=page_size, page_token=page_token
        )
        return self._json(
            {
                "ok": True,
                "action": "list_blocks",
                "doc_token": doc_token,
                "page_size": page_size,
                "page_token": next_token,
                "has_more": has_more,
                "items": [self._to_jsonable(item) for item in items],
            }
        )

    def _get_block(self, client: Any, doc_token: str, block_id: str) -> str:
        block = self._get_document_block(client, doc_token, block_id)
        block_type = int(getattr(block, "block_type", 0) or 0)
        return self._json(
            {
                "ok": True,
                "action": "get_block",
                "doc_token": doc_token,
                "block_id": block_id,
                "block_type": block_type,
                "block_type_name": _DOCX_BLOCK_TYPE_NAMES.get(block_type, f"type_{block_type}"),
                "text": self._extract_block_text(block) or None,
                "block": self._to_jsonable(block),
            }
        )

    def _update_block_text(self, client: Any, doc_token: str, block_id: str, content: str) -> str:
        block = self._get_document_block(client, doc_token, block_id)
        text_run = lark_docx.TextRun.builder().content(content).build()
        element = lark_docx.TextElement.builder().text_run(text_run).build()
        update_text_elements = (
            lark_docx.UpdateTextElementsRequest.builder()
            .elements([element])
            .build()
        )
        body = (
            lark_docx.UpdateBlockRequest.builder()
            .update_text_elements(update_text_elements)
            .build()
        )
        req = (
            lark_docx.PatchDocumentBlockRequest.builder()
            .document_id(doc_token)
            .block_id(block_id)
            .client_token(str(uuid.uuid4()))
            .request_body(body)
            .build()
        )
        resp = client.docx.v1.document_block.patch(req)
        self._require_success(resp, "Update DocX block")
        block_type = int(getattr(block, "block_type", 0) or 0)
        return self._json(
            {
                "ok": True,
                "action": "update_block",
                "doc_token": doc_token,
                "block_id": block_id,
                "block_type": block_type,
                "block_type_name": _DOCX_BLOCK_TYPE_NAMES.get(block_type, f"type_{block_type}"),
                "content_length": len(content),
            }
        )

    def _delete_block(self, client: Any, doc_token: str, block_id: str) -> str:
        if (block_id or "").strip() == (doc_token or "").strip():
            raise RuntimeError(
                "Refusing to delete the document root block. "
                "Use feishu_doc action=write to replace content, or list_blocks/get_block to target a child block_id."
            )
        block = self._get_document_block(client, doc_token, block_id)
        parent_id = (getattr(block, "parent_id", None) or doc_token).strip()
        index = self._find_child_index(client, doc_token, parent_id, block_id)
        if index is None:
            raise RuntimeError("Block not found in parent children list")
        body = (
            lark_docx.BatchDeleteDocumentBlockChildrenRequestBody.builder()
            .start_index(index)
            .end_index(index + 1)
            .build()
        )
        req = (
            lark_docx.BatchDeleteDocumentBlockChildrenRequest.builder()
            .document_id(doc_token)
            .block_id(parent_id)
            .client_token(str(uuid.uuid4()))
            .request_body(body)
            .build()
        )
        resp = client.docx.v1.document_block_children.batch_delete(req)
        self._require_success(resp, "Delete DocX block")
        block_type = int(getattr(block, "block_type", 0) or 0)
        return self._json(
            {
                "ok": True,
                "action": "delete_block",
                "doc_token": doc_token,
                "deleted_block_id": block_id,
                "parent_id": parent_id,
                "index": index,
                "block_type": block_type,
                "block_type_name": _DOCX_BLOCK_TYPE_NAMES.get(block_type, f"type_{block_type}"),
            }
        )

    def _write_or_append_doc(
        self,
        client: Any,
        doc_token: str,
        markdown: str,
        replace: bool,
        *,
        force_chunked: bool = False,
        chunk_chars: int | None = None,
    ) -> str:
        deleted_count = 0
        if replace:
            deleted_count = self._clear_document_top_level_content(client, doc_token)

        stripped = markdown.strip()
        if not stripped:
            return self._json(
                {
                    "ok": True,
                    "action": "write" if replace else "append",
                    "doc_token": doc_token,
                    "deleted_top_level_blocks": deleted_count,
                    "inserted_blocks": 0,
                    "skipped_block_types": [],
                }
            )

        chunk_size = max(
            _DOCX_WRITE_CHUNK_CHARS_MIN,
            min(int(chunk_chars or _DOCX_WRITE_CHUNK_CHARS_DEFAULT), _DOCX_WRITE_CHUNK_CHARS_MAX),
        )
        if force_chunked:
            chunked = self._insert_markdown_chunks(
                client=client,
                doc_token=doc_token,
                markdown=stripped,
                chunk_chars=chunk_size,
            )
            return self._json(
                {
                    "ok": True,
                    "action": "write" if replace else "append",
                    "doc_token": doc_token,
                    "deleted_top_level_blocks": deleted_count,
                    "strategy": "chunked_forced",
                    **chunked,
                }
            )

        converted_blocks, first_level_ids = self._convert_markdown_to_blocks(client, stripped)
        ordered_blocks = self._sort_blocks_by_first_level(converted_blocks, first_level_ids)
        cleaned_blocks, skipped_types = self._clean_blocks_for_insert(ordered_blocks)
        if not cleaned_blocks:
            return self._json(
                {
                    "ok": True,
                    "action": "write" if replace else "append",
                    "doc_token": doc_token,
                    "deleted_top_level_blocks": deleted_count,
                    "inserted_blocks": 0,
                    "skipped_block_types": skipped_types,
                    "warning": "No insertable blocks after markdown conversion",
                }
            )

        try:
            children = self._insert_blocks_with_retry(client, doc_token, cleaned_blocks)
        except Exception as e:
            chunked = self._insert_markdown_chunks(
                client=client,
                doc_token=doc_token,
                markdown=stripped,
                chunk_chars=chunk_size,
            )
            if int(chunked.get("successful_chunks", 0) or 0) <= 0:
                raise RuntimeError(
                    f"{e}. Chunked fallback also failed ({chunked.get('failed_chunks', 0)} chunks)."
                ) from e
            result: dict[str, Any] = {
                "ok": True,
                "action": "write" if replace else "append",
                "doc_token": doc_token,
                "deleted_top_level_blocks": deleted_count,
                "strategy": "chunked_fallback",
                "fallback_from_error": str(e),
                "first_level_block_ids": first_level_ids,
                "initial_converted_blocks": len(converted_blocks),
                **chunked,
            }
            if skipped_types:
                merged_skips = list(dict.fromkeys([*skipped_types, *chunked.get("skipped_block_types", [])]))
                result["skipped_block_types"] = merged_skips
            if int(chunked.get("failed_chunks", 0) or 0) > 0:
                result["warning"] = (
                    "Chunked write completed with partial failures; review failed_chunk_details."
                )
            return self._json(result)

        return self._json(
            {
                "ok": True,
                "action": "write" if replace else "append",
                "doc_token": doc_token,
                "deleted_top_level_blocks": deleted_count,
                "strategy": "single_batch",
                "converted_blocks": len(converted_blocks),
                "inserted_blocks": len(children),
                "skipped_block_types": skipped_types,
                "first_level_block_ids": first_level_ids,
            }
        )

    def _insert_blocks_with_retry(self, client: Any, doc_token: str, blocks: list[Any]) -> list[Any]:
        last_error: Exception | None = None
        for attempt in range(_DOCX_WRITE_MAX_RETRIES):
            try:
                return self._insert_blocks(client, doc_token, blocks)
            except Exception as e:
                last_error = e
                if attempt >= _DOCX_WRITE_MAX_RETRIES - 1:
                    raise
                if not self._is_retryable_docx_insert_error(e):
                    raise
                delay = min(2.0, _DOCX_WRITE_RETRY_BASE_SECONDS * (2 ** attempt))
                time.sleep(delay)
        if last_error is not None:
            raise last_error
        raise RuntimeError("Insert DocX blocks failed with unknown retry state")

    def _should_auto_force_chunked(self, action: str, content: str | None) -> bool:
        if action not in {"write", "append"}:
            return False
        if self.auto_chunk_threshold_chars <= 0:
            return False
        size = len((content or "").strip())
        return size >= self.auto_chunk_threshold_chars

    def _is_retryable_docx_insert_error(self, error: Exception) -> bool:
        text = str(error).lower()
        retry_markers = (
            "code=429",
            "too many requests",
            "rate limit",
            "code=99991400",
            "code=99991403",
            "timeout",
            "temporar",
            "code=1770014",
            "revision",
        )
        return any(marker in text for marker in retry_markers)

    def _insert_markdown_chunks(
        self,
        client: Any,
        doc_token: str,
        markdown: str,
        chunk_chars: int,
    ) -> dict[str, Any]:
        chunks = self._split_markdown_for_docx(markdown, chunk_chars)
        converted_total = 0
        inserted_total = 0
        successful = 0
        failed = 0
        skipped_all: list[str] = []
        failed_details: list[dict[str, Any]] = []

        for idx, chunk in enumerate(chunks, start=1):
            try:
                converted_blocks, first_level_ids = self._convert_markdown_to_blocks(client, chunk)
                converted_total += len(converted_blocks)
                ordered_blocks = self._sort_blocks_by_first_level(converted_blocks, first_level_ids)
                cleaned_blocks, skipped_types = self._clean_blocks_for_insert(ordered_blocks)
                skipped_all.extend(skipped_types)
                if not cleaned_blocks:
                    successful += 1
                    continue
                inserted = self._insert_blocks_with_retry(client, doc_token, cleaned_blocks)
                inserted_total += len(inserted)
                successful += 1
            except Exception as e:
                failed += 1
                if len(failed_details) < 8:
                    failed_details.append(
                        {
                            "chunk_index": idx,
                            "chunk_chars": len(chunk),
                            "error": str(e),
                        }
                    )

        return {
            "chunk_chars": chunk_chars,
            "chunk_count": len(chunks),
            "successful_chunks": successful,
            "failed_chunks": failed,
            "converted_blocks": converted_total,
            "inserted_blocks": inserted_total,
            "skipped_block_types": list(dict.fromkeys(skipped_all)),
            "failed_chunk_details": failed_details,
        }

    def _split_markdown_for_docx(self, markdown: str, max_chars: int) -> list[str]:
        text = (markdown or "").strip()
        if not text:
            return []
        if len(text) <= max_chars:
            return [text]

        sections = [seg.strip() for seg in re.split(r"(?m)(?=^#{1,6}\s)", text) if seg.strip()]
        if len(sections) <= 1:
            sections = [seg.strip() for seg in re.split(r"\n{2,}", text) if seg.strip()]
        if not sections:
            sections = [text]

        def _hard_split(section: str) -> list[str]:
            if len(section) <= max_chars:
                return [section]
            lines = section.splitlines()
            if not lines:
                lines = [section]
            parts: list[str] = []
            current: list[str] = []
            current_len = 0

            def _flush_current() -> None:
                nonlocal current, current_len
                if current:
                    part = "\n".join(current).strip()
                    if part:
                        parts.append(part)
                current = []
                current_len = 0

            def _append_piece(piece: str) -> None:
                nonlocal current_len
                piece = piece.rstrip("\n")
                if not piece and not current:
                    return
                piece_len = len(piece)
                join_overhead = 1 if current else 0
                candidate_len = current_len + join_overhead + piece_len
                if current and candidate_len > max_chars:
                    _flush_current()
                    join_overhead = 0
                    candidate_len = piece_len
                if piece_len > max_chars:
                    # Keep large atomic blocks intact (especially markdown tables).
                    _flush_current()
                    stripped = piece.strip()
                    if stripped:
                        parts.append(stripped)
                    return
                current.append(piece)
                current_len = candidate_len

            idx = 0
            while idx < len(lines):
                line = lines[idx]
                if (
                    idx + 1 < len(lines)
                    and _DOCX_MD_TABLE_ROW_RE.match(line or "")
                    and _DOCX_MD_TABLE_SEPARATOR_RE.match(lines[idx + 1] or "")
                ):
                    table_lines = [line, lines[idx + 1]]
                    idx += 2
                    while idx < len(lines) and _DOCX_MD_TABLE_ROW_RE.match(lines[idx] or ""):
                        table_lines.append(lines[idx])
                        idx += 1
                    _append_piece("\n".join(table_lines))
                    continue
                _append_piece(line)
                idx += 1

            _flush_current()
            return [p for p in parts if p]

        chunks: list[str] = []
        buffer = ""
        for section in sections:
            for part in _hard_split(section):
                candidate = part if not buffer else f"{buffer}\n\n{part}"
                if len(candidate) <= max_chars:
                    buffer = candidate
                else:
                    if buffer:
                        chunks.append(buffer.strip())
                    buffer = part
        if buffer:
            chunks.append(buffer.strip())
        return [c for c in chunks if c]

    def _sort_blocks_by_first_level(self, blocks: list[Any], first_level_ids: list[str]) -> list[Any]:
        if not first_level_ids:
            return blocks

        by_id: dict[str, Any] = {}
        for block in blocks:
            block_id = getattr(block, "block_id", None)
            if block_id:
                by_id[str(block_id)] = block

        ordered: list[Any] = [by_id[block_id] for block_id in first_level_ids if block_id in by_id]
        first_level_set = {str(block_id) for block_id in first_level_ids}
        remaining = [
            block
            for block in blocks
            if str(getattr(block, "block_id", "")) not in first_level_set
        ]
        return ordered + remaining

    def _convert_markdown_to_blocks(self, client: Any, markdown: str) -> tuple[list[Any], list[str]]:
        body = (
            lark_docx.ConvertDocumentRequestBody.builder()
            .content_type("markdown")
            .content(markdown)
            .build()
        )
        req = lark_docx.ConvertDocumentRequest.builder().request_body(body).build()
        resp = client.docx.v1.document.convert(req)
        self._require_success(resp, "Convert markdown to DocX blocks")
        data = getattr(resp, "data", None)
        blocks = list(getattr(data, "blocks", None) or [])
        first_level = list(getattr(data, "first_level_block_ids", None) or [])
        return blocks, first_level

    def _clean_blocks_for_insert(self, blocks: list[Any]) -> tuple[list[Any], list[str]]:
        cleaned: list[Any] = []
        skipped: list[str] = []
        for block in blocks:
            block_dict = self._to_jsonable(block)
            if not isinstance(block_dict, dict):
                continue
            block_type = int(block_dict.get("block_type", 0) or 0)
            if block_type in _DOCX_UNSUPPORTED_CREATE_TYPES:
                skipped.append(_DOCX_BLOCK_TYPE_NAMES.get(block_type, f"type_{block_type}"))
                continue
            # Keep converted block hierarchy/ids from Feishu's markdown convert output.
            # Image insertion relies on nested block structure (e.g. image placeholder blocks).
            block_dict.pop("comment_ids", None)
            if block_type == 31 and isinstance(block_dict.get("table"), dict):
                block_dict["table"].pop("merge_info", None)
            cleaned.append(lark_docx.Block(block_dict))
        return cleaned, skipped

    def _insert_blocks(self, client: Any, doc_token: str, blocks: list[Any]) -> list[Any]:
        body = lark_docx.CreateDocumentBlockChildrenRequestBody.builder().children(blocks).build()
        req = (
            lark_docx.CreateDocumentBlockChildrenRequest.builder()
            .document_id(doc_token)
            .block_id(doc_token)
            .client_token(str(uuid.uuid4()))
            .request_body(body)
            .build()
        )
        resp = client.docx.v1.document_block_children.create(req)
        self._require_success(resp, "Insert DocX blocks")
        return list(getattr(getattr(resp, "data", None), "children", None) or [])

    def _clear_document_top_level_content(self, client: Any, doc_token: str) -> int:
        blocks = self._list_all_document_blocks(client, doc_token)
        top_level = [
            block
            for block in blocks
            if getattr(block, "parent_id", None) == doc_token and int(getattr(block, "block_type", 0) or 0) != 1
        ]
        if not top_level:
            return 0
        body = (
            lark_docx.BatchDeleteDocumentBlockChildrenRequestBody.builder()
            .start_index(0)
            .end_index(len(top_level))
            .build()
        )
        req = (
            lark_docx.BatchDeleteDocumentBlockChildrenRequest.builder()
            .document_id(doc_token)
            .block_id(doc_token)
            .client_token(str(uuid.uuid4()))
            .request_body(body)
            .build()
        )
        resp = client.docx.v1.document_block_children.batch_delete(req)
        self._require_success(resp, "Clear DocX content")
        return len(top_level)

    def _list_all_document_blocks(self, client: Any, doc_token: str) -> list[Any]:
        out: list[Any] = []
        page_token: str | None = None
        while True:
            items, next_token, has_more = self._list_document_blocks_page(
                client, doc_token, page_size=500, page_token=page_token
            )
            out.extend(items)
            if not has_more:
                break
            page_token = next_token
            if not page_token:
                break
            if len(out) >= 5000:
                break
        return out

    def _list_document_blocks_page(
        self,
        client: Any,
        doc_token: str,
        page_size: int,
        page_token: str | None,
    ) -> tuple[list[Any], str | None, bool]:
        builder = (
            lark_docx.ListDocumentBlockRequest.builder()
            .document_id(doc_token)
            .page_size(page_size)
        )
        if page_token:
            builder = builder.page_token(page_token)
        req = builder.build()
        resp = client.docx.v1.document_block.list(req)
        self._require_success(resp, "List DocX blocks")
        data = getattr(resp, "data", None)
        items = list(getattr(data, "items", None) or [])
        has_more = bool(getattr(data, "has_more", False))
        next_token = getattr(data, "page_token", None)
        return items, next_token, has_more

    def _get_document_block(self, client: Any, doc_token: str, block_id: str) -> Any:
        req = (
            lark_docx.GetDocumentBlockRequest.builder()
            .document_id(doc_token)
            .block_id(block_id)
            .build()
        )
        resp = client.docx.v1.document_block.get(req)
        self._require_success(resp, "Get DocX block")
        block = getattr(getattr(resp, "data", None), "block", None)
        if block is None:
            raise RuntimeError("DocX block response missing block data")
        return block

    def _find_child_index(self, client: Any, doc_token: str, parent_id: str, block_id: str) -> int | None:
        page_token: str | None = None
        base_index = 0
        while True:
            items, next_token, has_more = self._list_block_children_page(
                client, doc_token, parent_id, page_size=500, page_token=page_token
            )
            for idx, item in enumerate(items):
                if getattr(item, "block_id", None) == block_id:
                    return base_index + idx
            if not has_more or not next_token:
                return None
            base_index += len(items)
            page_token = next_token

    def _list_block_children_page(
        self,
        client: Any,
        doc_token: str,
        block_id: str,
        page_size: int,
        page_token: str | None,
    ) -> tuple[list[Any], str | None, bool]:
        builder = (
            lark_docx.GetDocumentBlockChildrenRequest.builder()
            .document_id(doc_token)
            .block_id(block_id)
            .page_size(page_size)
        )
        if page_token:
            builder = builder.page_token(page_token)
        req = builder.build()
        resp = client.docx.v1.document_block_children.get(req)
        self._require_success(resp, "Get DocX block children")
        data = getattr(resp, "data", None)
        items = list(getattr(data, "items", None) or [])
        has_more = bool(getattr(data, "has_more", False))
        next_token = getattr(data, "page_token", None)
        return items, next_token, has_more

    def _doc_summary(self, doc: Any) -> dict[str, Any] | None:
        if not doc:
            return None
        return {
            "document_id": getattr(doc, "document_id", None),
            "title": getattr(doc, "title", None),
            "revision_id": getattr(doc, "revision_id", None),
        }

    def _extract_block_text(self, block: Any) -> str:
        for attr in (
            "text",
            "heading1",
            "heading2",
            "heading3",
            "heading4",
            "heading5",
            "heading6",
            "heading7",
            "heading8",
            "heading9",
            "bullet",
            "ordered",
            "code",
            "quote",
            "todo",
        ):
            text_obj = getattr(block, attr, None)
            text = self._extract_text_object(text_obj)
            if text:
                return text
        return ""

    def _extract_text_object(self, text_obj: Any) -> str:
        if not text_obj:
            return ""
        parts: list[str] = []
        for el in getattr(text_obj, "elements", None) or []:
            text_run = getattr(el, "text_run", None)
            content = getattr(text_run, "content", None)
            if content:
                parts.append(str(content))
        return "".join(parts).strip()


class FeishuWikiTool(_FeishuSdkToolBase):
    """Feishu Wiki (knowledge base) operations."""

    _WIKI_PAGE_SIZE_MAX = 50

    @property
    def name(self) -> str:
        return "feishu_wiki"

    @property
    def description(self) -> str:
        return (
            "Feishu knowledge base (Wiki) operations. Actions: spaces, nodes, get, create, move, rename. "
            "Use this to discover valid space_id/node_token before feishu_doc create. "
            "If permission denied, ensure the bot (or a group containing the bot) is a Wiki space member."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        page_size_schema = {
            "type": "integer",
            "minimum": 1,
            "maximum": self._WIKI_PAGE_SIZE_MAX,
            "description": "Page size (optional).",
        }
        space_id_schema = {
            "description": "Wiki space_id.",
            "anyOf": [{"type": "string"}, {"type": "integer"}],
        }
        page_token_schema = {"type": "string", "description": "Pagination token (optional)."}
        return {
            "type": "object",
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["spaces"]},
                        "page_size": page_size_schema,
                        "page_token": page_token_schema,
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["nodes"]},
                        "space_id": space_id_schema,
                        "parent_node_token": {
                            "type": "string",
                            "description": "Optional parent wiki node token. Omit for root.",
                        },
                        "page_size": page_size_schema,
                        "page_token": page_token_schema,
                    },
                    "required": ["action", "space_id"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["get"]},
                        "token": {"type": "string", "description": "Wiki node token (from /wiki/<token>)."},
                        "url": {"type": "string", "description": "Optional Wiki URL (extracts /wiki/<token>)."},
                    },
                    "required": ["action"],
                    "anyOf": [{"required": ["token"]}, {"required": ["url"]}],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["create"]},
                        "space_id": space_id_schema,
                        "title": {"type": "string", "description": "Node title."},
                        "obj_type": {
                            "type": "string",
                            "enum": ["docx", "sheet", "bitable"],
                            "description": "Object type (default docx).",
                        },
                        "parent_node_token": {
                            "type": "string",
                            "description": "Optional parent wiki node token.",
                        },
                    },
                    "required": ["action", "space_id", "title"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["move"]},
                        "space_id": {
                            "description": "Source Wiki space_id.",
                            "anyOf": [{"type": "string"}, {"type": "integer"}],
                        },
                        "node_token": {"type": "string", "description": "Wiki node token to move."},
                        "target_space_id": {
                            "description": "Target space_id (optional; same space if omitted).",
                            "anyOf": [{"type": "string"}, {"type": "integer"}],
                        },
                        "target_parent_token": {
                            "type": "string",
                            "description": "Target parent node token (optional; root if omitted).",
                        },
                    },
                    "required": ["action", "space_id", "node_token"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["rename"]},
                        "space_id": space_id_schema,
                        "node_token": {"type": "string", "description": "Wiki node token."},
                        "title": {"type": "string", "description": "New title."},
                    },
                    "required": ["action", "space_id", "node_token", "title"],
                    "additionalProperties": False,
                },
            ],
        }

    async def execute(
        self,
        action: str,
        space_id: str | int | None = None,
        parent_node_token: str | None = None,
        token: str | None = None,
        url: str | None = None,
        title: str | None = None,
        obj_type: str | None = None,
        node_token: str | None = None,
        target_space_id: str | int | None = None,
        target_parent_token: str | None = None,
        page_size: int = 50,
        page_token: str | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            if lark_wiki_v2 is None:
                raise RuntimeError("Wiki SDK not available in current lark-oapi installation")
            client = self._create_client()
            space_id_value = str(space_id).strip() if space_id is not None else ""
            target_space_id_value = str(target_space_id).strip() if target_space_id is not None else ""
            if action == "spaces":
                return self._list_spaces(client, page_size=page_size, page_token=page_token)
            if action == "nodes":
                if not space_id_value:
                    return "Error: space_id is required for feishu_wiki action=nodes"
                return self._list_nodes(
                    client,
                    space_id=space_id_value,
                    parent_node_token=(parent_node_token or "").strip() or None,
                    page_size=page_size,
                    page_token=(page_token or "").strip() or None,
                )
            if action == "get":
                node_tok = self._resolve_wiki_node_token(token=token, url=url)
                return self._get_node(client, node_tok)
            if action == "create":
                if not space_id_value:
                    return "Error: space_id is required for feishu_wiki action=create"
                if not (title or "").strip():
                    return "Error: title is required for feishu_wiki action=create"
                return self._create_node(
                    client,
                    space_id=space_id_value,
                    title=title.strip(),
                    obj_type=(obj_type or "docx").strip() or "docx",
                    parent_node_token=(parent_node_token or "").strip() or None,
                )
            if action == "move":
                if not space_id_value:
                    return "Error: space_id is required for feishu_wiki action=move"
                if not (node_token or "").strip():
                    return "Error: node_token is required for feishu_wiki action=move"
                return self._move_node(
                    client,
                    space_id=space_id_value,
                    node_token=node_token.strip(),
                    target_space_id=target_space_id_value or None,
                    target_parent_token=(target_parent_token or "").strip() or None,
                )
            if action == "rename":
                if not space_id_value:
                    return "Error: space_id is required for feishu_wiki action=rename"
                if not (node_token or "").strip():
                    return "Error: node_token is required for feishu_wiki action=rename"
                if not (title or "").strip():
                    return "Error: title is required for feishu_wiki action=rename"
                return self._rename_node(
                    client,
                    space_id=space_id_value,
                    node_token=node_token.strip(),
                    title=title.strip(),
                )
            return f"Error: Unsupported feishu_wiki action={action}"
        except Exception as e:
            msg = str(e)
            lower = msg.lower()
            if "131006" in msg or "permission denied" in lower:
                msg = f"{msg}. {_FEISHU_WIKI_ACCESS_HINT}"
            return f"Error: {msg}"

    def _resolve_wiki_node_token(self, token: str | None, url: str | None) -> str:
        if token and token.strip():
            return token.strip()
        if not url:
            raise ValueError("token (or url) is required")
        match = _WIKI_URL_RE.search(url)
        if not match:
            raise ValueError(f"Could not extract wiki node token from url: {url}")
        return match.group(1)

    def _list_spaces(self, client: Any, page_size: int, page_token: str | None) -> str:
        builder = lark_wiki_v2.ListSpaceRequest.builder().page_size(
            max(1, min(int(page_size or self._WIKI_PAGE_SIZE_MAX), self._WIKI_PAGE_SIZE_MAX))
        )
        if page_token:
            builder = builder.page_token(page_token)
        resp = client.wiki.v2.space.list(builder.build())
        self._require_success(resp, "List wiki spaces")
        data = getattr(resp, "data", None)
        items = list(getattr(data, "items", None) or [])
        spaces = [
            {
                "space_id": getattr(item, "space_id", None),
                "name": getattr(item, "name", None),
                "description": getattr(item, "description", None),
                "visibility": getattr(item, "visibility", None),
            }
            for item in items
        ]
        out: dict[str, Any] = {
            "ok": True,
            "action": "spaces",
            "items": spaces,
            "has_more": bool(getattr(data, "has_more", False)),
            "page_token": getattr(data, "page_token", None),
        }
        if not spaces:
            out["hint"] = _FEISHU_WIKI_ACCESS_HINT
        return self._json(out)

    def _list_nodes(
        self,
        client: Any,
        space_id: str,
        parent_node_token: str | None,
        page_size: int,
        page_token: str | None,
    ) -> str:
        builder = (
            lark_wiki_v2.ListSpaceNodeRequest.builder()
            .space_id(space_id)
            .page_size(max(1, min(int(page_size or self._WIKI_PAGE_SIZE_MAX), self._WIKI_PAGE_SIZE_MAX)))
        )
        if parent_node_token:
            builder = builder.parent_node_token(parent_node_token)
        if page_token:
            builder = builder.page_token(page_token)
        resp = client.wiki.v2.space_node.list(builder.build())
        self._require_success(resp, "List wiki nodes")
        data = getattr(resp, "data", None)
        items = list(getattr(data, "items", None) or [])
        nodes = [
            {
                "node_token": getattr(item, "node_token", None),
                "obj_token": getattr(item, "obj_token", None),
                "obj_type": getattr(item, "obj_type", None),
                "title": getattr(item, "title", None),
                "parent_node_token": getattr(item, "parent_node_token", None),
                "has_child": getattr(item, "has_child", None),
            }
            for item in items
        ]
        return self._json(
            {
                "ok": True,
                "action": "nodes",
                "space_id": space_id,
                "parent_node_token": parent_node_token,
                "items": nodes,
                "has_more": bool(getattr(data, "has_more", False)),
                "page_token": getattr(data, "page_token", None),
            }
        )

    def _get_node(self, client: Any, token: str) -> str:
        req = lark_wiki_v2.GetNodeSpaceRequest.builder().token(token).build()
        resp = client.wiki.v2.space.get_node(req)
        self._require_success(resp, "Get wiki node")
        node = getattr(getattr(resp, "data", None), "node", None)
        obj_type = getattr(node, "obj_type", None)
        obj_token = getattr(node, "obj_token", None)
        out = {
            "ok": True,
            "action": "get",
            "node": {
                "node_token": getattr(node, "node_token", None),
                "space_id": getattr(node, "space_id", None),
                "obj_token": obj_token,
                "obj_type": obj_type,
                "title": getattr(node, "title", None),
                "parent_node_token": getattr(node, "parent_node_token", None),
                "has_child": getattr(node, "has_child", None),
                "creator": self._to_jsonable(getattr(node, "creator", None)),
                "create_time": getattr(node, "node_create_time", None),
                "wiki_url": f"https://feishu.cn/wiki/{getattr(node, 'node_token', None)}"
                if getattr(node, "node_token", None)
                else None,
                "docx_url": f"https://feishu.cn/docx/{obj_token}" if obj_type == "docx" and obj_token else None,
            },
        }
        return self._json(out)

    def _create_node(
        self,
        client: Any,
        space_id: str,
        title: str,
        obj_type: str,
        parent_node_token: str | None,
    ) -> str:
        obj_type = obj_type if obj_type in {"docx", "sheet", "bitable"} else "docx"
        node_builder = lark_wiki_v2.Node.builder().obj_type(obj_type).node_type("origin").title(title)
        if parent_node_token:
            node_builder = node_builder.parent_node_token(parent_node_token)
        req = (
            lark_wiki_v2.CreateSpaceNodeRequest.builder()
            .space_id(space_id)
            .request_body(node_builder.build())
            .build()
        )
        resp = client.wiki.v2.space_node.create(req)
        self._require_success(resp, "Create wiki node")
        node = getattr(getattr(resp, "data", None), "node", None)
        node_token = getattr(node, "node_token", None)
        obj_token = getattr(node, "obj_token", None)
        return self._json(
            {
                "ok": True,
                "action": "create",
                "node": {
                    "space_id": getattr(node, "space_id", None) or space_id,
                    "node_token": node_token,
                    "parent_node_token": getattr(node, "parent_node_token", None) or parent_node_token,
                    "obj_type": getattr(node, "obj_type", None) or obj_type,
                    "obj_token": obj_token,
                    "title": getattr(node, "title", None) or title,
                    "wiki_url": f"https://feishu.cn/wiki/{node_token}" if node_token else None,
                    "docx_url": f"https://feishu.cn/docx/{obj_token}" if (obj_type == "docx" and obj_token) else None,
                },
            }
        )

    def _move_node(
        self,
        client: Any,
        space_id: str,
        node_token: str,
        target_space_id: str | None,
        target_parent_token: str | None,
    ) -> str:
        body_builder = lark_wiki_v2.MoveSpaceNodeRequestBody.builder().target_space_id(target_space_id or space_id)
        if target_parent_token:
            body_builder = body_builder.target_parent_token(target_parent_token)
        req = (
            lark_wiki_v2.MoveSpaceNodeRequest.builder()
            .space_id(space_id)
            .node_token(node_token)
            .request_body(body_builder.build())
            .build()
        )
        resp = client.wiki.v2.space_node.move(req)
        self._require_success(resp, "Move wiki node")
        moved = getattr(getattr(resp, "data", None), "node", None)
        return self._json(
            {
                "ok": True,
                "action": "move",
                "space_id": space_id,
                "node_token": getattr(moved, "node_token", None) or node_token,
                "target_space_id": target_space_id or space_id,
                "target_parent_token": target_parent_token,
            }
        )

    def _rename_node(self, client: Any, space_id: str, node_token: str, title: str) -> str:
        body = lark_wiki_v2.UpdateTitleSpaceNodeRequestBody.builder().title(title).build()
        req = (
            lark_wiki_v2.UpdateTitleSpaceNodeRequest.builder()
            .space_id(space_id)
            .node_token(node_token)
            .request_body(body)
            .build()
        )
        resp = client.wiki.v2.space_node.update_title(req)
        self._require_success(resp, "Rename wiki node")
        return self._json(
            {
                "ok": True,
                "action": "rename",
                "space_id": space_id,
                "node_token": node_token,
                "title": title,
            }
        )


class FeishuDriveTool(_FeishuSdkToolBase):
    """Feishu Drive file/folder operations."""

    @property
    def name(self) -> str:
        return "feishu_drive"

    @property
    def description(self) -> str:
        return (
            "Feishu cloud storage operations. Actions: list, info, create_folder, move, delete. "
            "Use this to browse folders and manage Drive files/docs."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        file_type_schema = {
            "type": "string",
            "enum": list(_FEISHU_DRIVE_FILE_TYPES),
            "description": "Feishu Drive file type (docx, sheet, folder, file, etc.).",
        }
        return {
            "type": "object",
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list"]},
                        "folder_token": {
                            "type": "string",
                            "description": "Optional folder token. Omit to list root.",
                        },
                        "page_size": {"type": "integer", "minimum": 1, "maximum": 200},
                        "page_token": {"type": "string"},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["info"]},
                        "file_token": {"type": "string", "description": "File token to find."},
                        "folder_token": {
                            "type": "string",
                            "description": "Optional folder token to search in (default root).",
                        },
                    },
                    "required": ["action", "file_token"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["create_folder"]},
                        "name": {"type": "string", "description": "Folder name to create."},
                        "folder_token": {
                            "type": "string",
                            "description": "Optional parent folder token. Omit for root.",
                        },
                    },
                    "required": ["action", "name"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["move"]},
                        "file_token": {"type": "string", "description": "File token to move."},
                        "type": file_type_schema,
                        "folder_token": {"type": "string", "description": "Target folder token."},
                    },
                    "required": ["action", "file_token", "type", "folder_token"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["delete"]},
                        "file_token": {"type": "string", "description": "File token to delete."},
                        "type": file_type_schema,
                    },
                    "required": ["action", "file_token", "type"],
                    "additionalProperties": False,
                },
            ],
        }

    async def execute(
        self,
        action: str,
        folder_token: str | None = None,
        file_token: str | None = None,
        type: str | None = None,
        name: str | None = None,
        page_size: int = 200,
        page_token: str | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            if not FEISHU_DRIVE_SDK_AVAILABLE or lark_drive is None:
                raise RuntimeError("Feishu Drive SDK not available in current lark-oapi installation")
            client = self._create_client()
            if action == "list":
                return self._list_files(
                    client,
                    folder_token=(folder_token or "").strip() or None,
                    page_size=max(1, min(page_size or 200, 200)),
                    page_token=(page_token or "").strip() or None,
                )
            if action == "info":
                tok = (file_token or "").strip()
                if not tok:
                    return "Error: file_token is required for feishu_drive action=info"
                return self._get_file_info(
                    client,
                    file_token=tok,
                    folder_token=(folder_token or "").strip() or None,
                )
            if action == "create_folder":
                folder_name = (name or "").strip()
                if not folder_name:
                    return "Error: name is required for feishu_drive action=create_folder"
                return self._create_folder(
                    client,
                    name=folder_name,
                    folder_token=(folder_token or "").strip() or None,
                )
            if action == "move":
                tok = (file_token or "").strip()
                tok_type = (type or "").strip()
                target_folder = (folder_token or "").strip()
                if not tok or not tok_type or not target_folder:
                    return "Error: file_token, type, folder_token are required for feishu_drive action=move"
                return self._move_file(client, file_token=tok, file_type=tok_type, folder_token=target_folder)
            if action == "delete":
                tok = (file_token or "").strip()
                tok_type = (type or "").strip()
                if not tok or not tok_type:
                    return "Error: file_token and type are required for feishu_drive action=delete"
                return self._delete_file(client, file_token=tok, file_type=tok_type)
            return f"Error: Unsupported feishu_drive action={action}"
        except Exception as e:
            return f"Error: {e}"

    def _list_files(
        self,
        client: Any,
        folder_token: str | None = None,
        page_size: int = 200,
        page_token: str | None = None,
    ) -> str:
        builder = lark_drive.ListFileRequest.builder().page_size(page_size)
        normalized_folder = self._normalize_folder_token(folder_token)
        if normalized_folder:
            builder = builder.folder_token(normalized_folder)
        if page_token:
            builder = builder.page_token(page_token)
        resp = client.drive.v1.file.list(builder.build())
        self._require_success(resp, "List Feishu Drive files")
        data = getattr(resp, "data", None)
        files = []
        for item in getattr(data, "files", None) or []:
            files.append(
                {
                    "token": getattr(item, "token", None),
                    "name": getattr(item, "name", None),
                    "type": getattr(item, "type", None),
                    "url": getattr(item, "url", None),
                    "parent_token": getattr(item, "parent_token", None),
                    "created_time": getattr(item, "created_time", None),
                    "modified_time": getattr(item, "modified_time", None),
                    "owner_id": getattr(item, "owner_id", None),
                }
            )
        return self._json(
            {
                "ok": True,
                "action": "list",
                "folder_token": normalized_folder,
                "files": files,
                "has_more": bool(getattr(data, "has_more", False)),
                "next_page_token": getattr(data, "next_page_token", None),
            }
        )

    def _get_file_info(self, client: Any, file_token: str, folder_token: str | None = None) -> str:
        # The v1 SDK does not expose a direct "get file meta by token" endpoint in this surface,
        # so we search the specified folder (or root) via list.
        list_result = json.loads(self._list_files(client, folder_token=folder_token, page_size=200))
        files = list_result.get("files") or []
        matched = next((f for f in files if f.get("token") == file_token), None)
        if not matched:
            raise RuntimeError(
                f"File not found: {file_token}. Try feishu_drive action=list on the correct folder first."
            )
        return self._json(
            {
                "ok": True,
                "action": "info",
                "file": matched,
                "searched_folder_token": list_result.get("folder_token"),
            }
        )

    def _create_folder(self, client: Any, name: str, folder_token: str | None = None) -> str:
        parent_token = self._normalize_folder_token(folder_token, for_create=True) or "0"
        body = lark_drive.CreateFolderFileRequestBody.builder().name(name).folder_token(parent_token).build()
        req = lark_drive.CreateFolderFileRequest.builder().request_body(body).build()
        resp = client.drive.v1.file.create_folder(req)
        self._require_success(resp, "Create Feishu Drive folder")
        data = getattr(resp, "data", None)
        return self._json(
            {
                "ok": True,
                "action": "create_folder",
                "name": name,
                "parent_folder_token": parent_token,
                "token": getattr(data, "token", None),
                "url": getattr(data, "url", None),
            }
        )

    def _move_file(self, client: Any, file_token: str, file_type: str, folder_token: str) -> str:
        body = lark_drive.MoveFileRequestBody.builder().type(file_type).folder_token(folder_token).build()
        req = (
            lark_drive.MoveFileRequest.builder()
            .file_token(file_token)
            .request_body(body)
            .build()
        )
        resp = client.drive.v1.file.move(req)
        self._require_success(resp, "Move Feishu Drive file")
        data = getattr(resp, "data", None)
        return self._json(
            {
                "ok": True,
                "action": "move",
                "file_token": file_token,
                "type": file_type,
                "folder_token": folder_token,
                "task_id": getattr(data, "task_id", None),
            }
        )

    def _delete_file(self, client: Any, file_token: str, file_type: str) -> str:
        req = (
            lark_drive.DeleteFileRequest.builder()
            .file_token(file_token)
            .type(file_type)
            .build()
        )
        resp = client.drive.v1.file.delete(req)
        self._require_success(resp, "Delete Feishu Drive file")
        data = getattr(resp, "data", None)
        return self._json(
            {
                "ok": True,
                "action": "delete",
                "file_token": file_token,
                "type": file_type,
                "task_id": getattr(data, "task_id", None),
            }
        )

    @staticmethod
    def _normalize_folder_token(folder_token: str | None, *, for_create: bool = False) -> str | None:
        token = (folder_token or "").strip()
        if not token:
            return None if not for_create else "0"
        if token == "0":
            return "0" if for_create else None
        return token


class FeishuAppScopesTool(_FeishuSdkToolBase):
    """List current Feishu app permission scopes."""

    @property
    def name(self) -> str:
        return "feishu_app_scopes"

    @property
    def description(self) -> str:
        return (
            "List current Feishu app permission scopes (granted/pending). "
            "Use this to debug missing Feishu API permissions."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, **kwargs: Any) -> str:
        try:
            if not FEISHU_APPLICATION_SDK_AVAILABLE or lark_application_v6 is None:
                raise RuntimeError("Feishu application scope SDK not available in current lark-oapi installation")
            client = self._create_client()
            req = lark_application_v6.ListScopeRequest.builder().build()
            resp = client.application.v6.scope.list(req)
            self._require_success(resp, "List app scopes")
            data = getattr(resp, "data", None)

            app_scopes = getattr(data, "app_scopes", None) or getattr(data, "scopes", None)
            scope_items: list[Any] = []
            if app_scopes is not None:
                for attr in ("high_level_scopes", "low_level_scopes", "scopes"):
                    vals = getattr(app_scopes, attr, None)
                    if vals:
                        scope_items.extend(list(vals))
            if not scope_items and data is not None:
                for attr in ("high_level_scopes", "low_level_scopes", "scopes"):
                    vals = getattr(data, attr, None)
                    if vals:
                        scope_items.extend(list(vals))

            granted: list[dict[str, Any]] = []
            pending: list[dict[str, Any]] = []
            seen: set[tuple[Any, Any, Any]] = set()
            for item in scope_items:
                row = {
                    "scope_name": getattr(item, "scope_name", None),
                    "scope_type": getattr(item, "scope_type", None),
                    "grant_status": getattr(item, "grant_status", None),
                }
                key = (row["scope_name"], row["scope_type"], row["grant_status"])
                if key in seen:
                    continue
                seen.add(key)
                if row["grant_status"] == 1:
                    granted.append(row)
                else:
                    pending.append(row)

            result: dict[str, Any] = {
                "ok": True,
                "action": "list",
                "granted": granted,
                "pending": pending,
                "summary": f"{len(granted)} granted, {len(pending)} pending",
            }
            if not granted and not pending:
                result["raw"] = self._to_jsonable(data)
            return self._json(result)
        except Exception as e:
            return f"Error: {e}"


class FeishuPermTool(_FeishuSdkToolBase):
    """Feishu Drive permission membership operations."""

    @property
    def name(self) -> str:
        return "feishu_perm"

    @property
    def description(self) -> str:
        return (
            "Feishu permission management for docx/wiki/folder/file tokens. Actions: list, add, remove. "
            "Use member_type=groupid to grant access via a group when the bot cannot be selected in the UI."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        token_type_schema = {
            "type": "string",
            "enum": list(_FEISHU_PERM_TOKEN_TYPES),
            "description": "Token type (e.g. docx, wiki, folder).",
        }
        member_type_schema = {
            "type": "string",
            "enum": list(_FEISHU_PERM_MEMBER_TYPES),
            "description": "Member type (openid, groupid, etc.).",
        }
        return {
            "type": "object",
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list"]},
                        "token": {"type": "string", "description": "Target token."},
                        "type": token_type_schema,
                    },
                    "required": ["action", "token", "type"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["add"]},
                        "token": {"type": "string", "description": "Target token."},
                        "type": token_type_schema,
                        "member_type": member_type_schema,
                        "member_id": {"type": "string", "description": "Member ID."},
                        "perm": {
                            "type": "string",
                            "enum": list(_FEISHU_PERM_VALUES),
                            "description": "Permission level.",
                        },
                    },
                    "required": ["action", "token", "type", "member_type", "member_id", "perm"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["remove"]},
                        "token": {"type": "string", "description": "Target token."},
                        "type": token_type_schema,
                        "member_type": member_type_schema,
                        "member_id": {"type": "string", "description": "Member ID to remove."},
                    },
                    "required": ["action", "token", "type", "member_type", "member_id"],
                    "additionalProperties": False,
                },
            ],
        }

    async def execute(
        self,
        action: str,
        token: str | None = None,
        type: str | None = None,
        member_type: str | None = None,
        member_id: str | None = None,
        perm: str | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            if not FEISHU_DRIVE_SDK_AVAILABLE or lark_drive is None:
                raise RuntimeError("Feishu Drive SDK not available in current lark-oapi installation")
            client = self._create_client()
            tok = (token or "").strip()
            tok_type = (type or "").strip()
            if action == "list":
                if not tok or not tok_type:
                    return "Error: token and type are required for feishu_perm action=list"
                return self._list_members(client, tok, tok_type)
            if action == "add":
                if not tok or not tok_type or not (member_type or "").strip() or not (member_id or "").strip() or not (perm or "").strip():
                    return "Error: token, type, member_type, member_id, perm are required for feishu_perm action=add"
                return self._add_member(
                    client,
                    token=tok,
                    token_type=tok_type,
                    member_type=member_type.strip(),
                    member_id=member_id.strip(),
                    perm=perm.strip(),
                )
            if action == "remove":
                if not tok or not tok_type or not (member_type or "").strip() or not (member_id or "").strip():
                    return "Error: token, type, member_type, member_id are required for feishu_perm action=remove"
                return self._remove_member(
                    client,
                    token=tok,
                    token_type=tok_type,
                    member_type=member_type.strip(),
                    member_id=member_id.strip(),
                )
            return f"Error: Unsupported feishu_perm action={action}"
        except Exception as e:
            return f"Error: {e}"

    def _list_members(self, client: Any, token: str, token_type: str) -> str:
        req = (
            lark_drive.ListPermissionMemberRequest.builder()
            .token(token)
            .type(token_type)
            .build()
        )
        resp = client.drive.v1.permission_member.list(req)
        self._require_success(resp, "List Feishu permission members")
        items = list(getattr(getattr(resp, "data", None), "items", None) or [])
        members = []
        for item in items:
            members.append(
                {
                    "member_type": getattr(item, "member_type", None),
                    "member_id": getattr(item, "member_id", None),
                    "perm": getattr(item, "perm", None),
                    "name": getattr(item, "name", None),
                }
            )
        return self._json(
            {
                "ok": True,
                "action": "list",
                "token": token,
                "type": token_type,
                "items": members,
            }
        )

    def _add_member(
        self,
        client: Any,
        token: str,
        token_type: str,
        member_type: str,
        member_id: str,
        perm: str,
    ) -> str:
        body = (
            lark_drive.BaseMember.builder()
            .member_type(member_type)
            .member_id(member_id)
            .perm(perm)
            .build()
        )
        req = (
            lark_drive.CreatePermissionMemberRequest.builder()
            .token(token)
            .type(token_type)
            .need_notification(False)
            .request_body(body)
            .build()
        )
        resp = client.drive.v1.permission_member.create(req)
        self._require_success(resp, "Add Feishu permission member")
        return self._json(
            {
                "ok": True,
                "action": "add",
                "token": token,
                "type": token_type,
                "member_type": member_type,
                "member_id": member_id,
                "perm": perm,
                "member": self._to_jsonable(getattr(getattr(resp, "data", None), "member", None)),
            }
        )

    def _remove_member(
        self,
        client: Any,
        token: str,
        token_type: str,
        member_type: str,
        member_id: str,
    ) -> str:
        req = (
            lark_drive.DeletePermissionMemberRequest.builder()
            .token(token)
            .type(token_type)
            .member_type(member_type)
            .member_id(member_id)
            .build()
        )
        resp = client.drive.v1.permission_member.delete(req)
        self._require_success(resp, "Remove Feishu permission member")
        return self._json(
            {
                "ok": True,
                "action": "remove",
                "token": token,
                "type": token_type,
                "member_type": member_type,
                "member_id": member_id,
            }
        )


class FeishuBitableGetMetaTool(_FeishuSdkToolBase):
    """Parse Bitable URLs and optionally inspect app/tables."""

    _APP_TOKEN_RE = re.compile(r"/base/([A-Za-z0-9_-]+)")
    _TABLE_PATH_RE = re.compile(r"/table/([A-Za-z0-9_-]+)")

    @property
    def name(self) -> str:
        return "feishu_bitable_get_meta"

    @property
    def description(self) -> str:
        return (
            "Parse a Feishu Bitable URL and return app_token/table_id. "
            "If credentials are configured, can also list tables in the app."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Feishu Bitable/base URL."},
                "fetch_tables": {
                    "type": "boolean",
                    "description": "If true, try to list tables using Feishu API (default true).",
                },
            },
            "required": ["url"],
        }

    async def execute(self, url: str, fetch_tables: bool = True, **kwargs: Any) -> str:
        try:
            parsed = self._parse_bitable_url(url)
            warnings = list(parsed.pop("warnings", []))
            app_info = None
            tables = None

            app_token = parsed.get("app_token")
            if fetch_tables and app_token:
                if self.app_id and self.app_secret:
                    client = self._create_client()
                    app_info, tables = self._fetch_app_and_tables(client, app_token)
                else:
                    warnings.append("Credentials missing: returning parsed tokens only (no app/table lookup).")

            result = {"ok": True, "url": url, **parsed}
            if app_info is not None:
                result["app"] = app_info
            if tables is not None:
                result["tables"] = tables
            if warnings:
                result["warnings"] = warnings
            return self._json(result)
        except Exception as e:
            return f"Error: {e}"

    def _parse_bitable_url(self, url: str) -> dict[str, Any]:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        fragment_qs = parse_qs(parsed.fragment)
        warnings: list[str] = []

        app_token = None
        m = self._APP_TOKEN_RE.search(parsed.path)
        if m:
            app_token = m.group(1)

        table_id = None
        tm = self._TABLE_PATH_RE.search(parsed.path)
        if tm:
            table_id = tm.group(1)
        for key in ("table", "table_id", "tbl"):
            if not table_id:
                vals = qs.get(key) or fragment_qs.get(key)
                if vals:
                    table_id = vals[0]

        if "/wiki/" in parsed.path and not app_token:
            warnings.append(
                "Wiki-style URL detected. app_token not directly extractable from URL; use a /base/ URL "
                "or provide app_token/table_id to other tools."
            )

        return {
            "host": parsed.netloc,
            "path": parsed.path,
            "app_token": app_token,
            "table_id": table_id,
            "is_wiki_url": "/wiki/" in parsed.path,
            "warnings": warnings,
        }

    def _fetch_app_and_tables(self, client: Any, app_token: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        app_req = lark_bitable.GetAppRequest.builder().app_token(app_token).build()
        app_resp = client.bitable.v1.app.get(app_req)
        self._require_success(app_resp, "Get Bitable app")
        app = getattr(getattr(app_resp, "data", None), "app", None)
        app_info = {
            "app_token": getattr(app, "app_token", None),
            "name": getattr(app, "name", None),
            "revision": getattr(app, "revision", None),
        } if app else None

        tables: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            builder = lark_bitable.ListAppTableRequest.builder().app_token(app_token).page_size(200)
            if page_token:
                builder = builder.page_token(page_token)
            resp = client.bitable.v1.app_table.list(builder.build())
            self._require_success(resp, "List Bitable tables")
            data = getattr(resp, "data", None)
            for item in getattr(data, "items", None) or []:
                tables.append(
                    {
                        "table_id": getattr(item, "table_id", None),
                        "name": getattr(item, "name", None),
                        "revision": getattr(item, "revision", None),
                    }
                )
            if not getattr(data, "has_more", False):
                break
            page_token = getattr(data, "page_token", None)
            if not page_token:
                break
        return app_info, tables


class _FeishuBitableToolBase(_FeishuSdkToolBase):
    def _bitable_client(self) -> Any:
        return self._create_client()

    def _record_payload(self, record: Any) -> dict[str, Any]:
        return {
            "record_id": getattr(record, "record_id", None),
            "fields": getattr(record, "fields", None) or {},
            "record_url": getattr(record, "record_url", None),
            "shared_url": getattr(record, "shared_url", None),
            "created_time": getattr(record, "created_time", None),
            "last_modified_time": getattr(record, "last_modified_time", None),
        }

    def _build_record_request_body(self, fields: dict[str, Any]) -> Any:
        return lark_bitable.AppTableRecord.builder().fields(fields).build()


class FeishuBitableListFieldsTool(_FeishuBitableToolBase):
    @property
    def name(self) -> str:
        return "feishu_bitable_list_fields"

    @property
    def description(self) -> str:
        return "List fields (columns) of a Feishu Bitable table."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "app_token": {"type": "string"},
                "table_id": {"type": "string"},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 500},
                "page_token": {"type": "string"},
            },
            "required": ["app_token", "table_id"],
        }

    async def execute(
        self,
        app_token: str,
        table_id: str,
        page_size: int = 200,
        page_token: str | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            client = self._bitable_client()
            builder = (
                lark_bitable.ListAppTableFieldRequest.builder()
                .app_token(app_token)
                .table_id(table_id)
                .page_size(max(1, min(page_size or 200, 500)))
            )
            if page_token:
                builder = builder.page_token(page_token)
            resp = client.bitable.v1.app_table_field.list(builder.build())
            self._require_success(resp, "List Bitable fields")
            data = getattr(resp, "data", None)
            items = []
            for field in getattr(data, "items", None) or []:
                items.append(
                    {
                        "field_id": getattr(field, "field_id", None),
                        "field_name": getattr(field, "field_name", None),
                        "type": getattr(field, "type", None),
                        "ui_type": getattr(field, "ui_type", None),
                        "is_primary": getattr(field, "is_primary", None),
                        "is_hidden": getattr(field, "is_hidden", None),
                    }
                )
            return self._json(
                {
                    "ok": True,
                    "app_token": app_token,
                    "table_id": table_id,
                    "items": items,
                    "has_more": bool(getattr(data, "has_more", False)),
                    "page_token": getattr(data, "page_token", None),
                    "total": getattr(data, "total", None),
                }
            )
        except Exception as e:
            return f"Error: {e}"


class FeishuBitableListRecordsTool(_FeishuBitableToolBase):
    @property
    def name(self) -> str:
        return "feishu_bitable_list_records"

    @property
    def description(self) -> str:
        return "List records (rows) in a Feishu Bitable table."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "app_token": {"type": "string"},
                "table_id": {"type": "string"},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 500},
                "page_token": {"type": "string"},
                "view_id": {"type": "string"},
                "filter": {"type": "string", "description": "Bitable filter JSON string."},
                "sort": {"type": "string", "description": "Bitable sort JSON string."},
                "field_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of field names to return (serialized to API query JSON).",
                },
            },
            "required": ["app_token", "table_id"],
        }

    async def execute(
        self,
        app_token: str,
        table_id: str,
        page_size: int = 100,
        page_token: str | None = None,
        view_id: str | None = None,
        filter: str | None = None,
        sort: str | None = None,
        field_names: list[str] | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            client = self._bitable_client()
            builder = (
                lark_bitable.ListAppTableRecordRequest.builder()
                .app_token(app_token)
                .table_id(table_id)
                .page_size(max(1, min(page_size or 100, 500)))
            )
            if page_token:
                builder = builder.page_token(page_token)
            if view_id:
                builder = builder.view_id(view_id)
            if filter:
                builder = builder.filter(filter)
            if sort:
                builder = builder.sort(sort)
            if field_names:
                builder = builder.field_names(json.dumps(field_names, ensure_ascii=False))
            resp = client.bitable.v1.app_table_record.list(builder.build())
            self._require_success(resp, "List Bitable records")
            data = getattr(resp, "data", None)
            items = [self._record_payload(rec) for rec in (getattr(data, "items", None) or [])]
            return self._json(
                {
                    "ok": True,
                    "app_token": app_token,
                    "table_id": table_id,
                    "items": items,
                    "has_more": bool(getattr(data, "has_more", False)),
                    "page_token": getattr(data, "page_token", None),
                    "total": getattr(data, "total", None),
                }
            )
        except Exception as e:
            return f"Error: {e}"


class FeishuBitableGetRecordTool(_FeishuBitableToolBase):
    @property
    def name(self) -> str:
        return "feishu_bitable_get_record"

    @property
    def description(self) -> str:
        return "Get a single record by record_id from a Feishu Bitable table."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "app_token": {"type": "string"},
                "table_id": {"type": "string"},
                "record_id": {"type": "string"},
            },
            "required": ["app_token", "table_id", "record_id"],
        }

    async def execute(self, app_token: str, table_id: str, record_id: str, **kwargs: Any) -> str:
        try:
            client = self._bitable_client()
            req = (
                lark_bitable.GetAppTableRecordRequest.builder()
                .app_token(app_token)
                .table_id(table_id)
                .record_id(record_id)
                .build()
            )
            resp = client.bitable.v1.app_table_record.get(req)
            self._require_success(resp, "Get Bitable record")
            record = getattr(getattr(resp, "data", None), "record", None)
            return self._json(
                {
                    "ok": True,
                    "app_token": app_token,
                    "table_id": table_id,
                    "record": self._record_payload(record) if record else None,
                }
            )
        except Exception as e:
            return f"Error: {e}"


class FeishuBitableCreateRecordTool(_FeishuBitableToolBase):
    @property
    def name(self) -> str:
        return "feishu_bitable_create_record"

    @property
    def description(self) -> str:
        return "Create a new record (row) in a Feishu Bitable table."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "app_token": {"type": "string"},
                "table_id": {"type": "string"},
                "fields": {
                    "type": "object",
                    "description": "Record fields payload. Keys should match Bitable field names.",
                },
            },
            "required": ["app_token", "table_id", "fields"],
        }

    async def execute(
        self,
        app_token: str,
        table_id: str,
        fields: dict[str, Any],
        **kwargs: Any,
    ) -> str:
        try:
            client = self._bitable_client()
            req = (
                lark_bitable.CreateAppTableRecordRequest.builder()
                .app_token(app_token)
                .table_id(table_id)
                .client_token(str(uuid.uuid4()))
                .request_body(self._build_record_request_body(fields))
                .build()
            )
            resp = client.bitable.v1.app_table_record.create(req)
            self._require_success(resp, "Create Bitable record")
            record = getattr(getattr(resp, "data", None), "record", None)
            return self._json(
                {
                    "ok": True,
                    "app_token": app_token,
                    "table_id": table_id,
                    "record": self._record_payload(record) if record else None,
                }
            )
        except Exception as e:
            return f"Error: {e}"


class FeishuBitableUpdateRecordTool(_FeishuBitableToolBase):
    @property
    def name(self) -> str:
        return "feishu_bitable_update_record"

    @property
    def description(self) -> str:
        return "Update an existing record (row) in a Feishu Bitable table."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "app_token": {"type": "string"},
                "table_id": {"type": "string"},
                "record_id": {"type": "string"},
                "fields": {
                    "type": "object",
                    "description": "Record fields payload to update.",
                },
            },
            "required": ["app_token", "table_id", "record_id", "fields"],
        }

    async def execute(
        self,
        app_token: str,
        table_id: str,
        record_id: str,
        fields: dict[str, Any],
        **kwargs: Any,
    ) -> str:
        try:
            client = self._bitable_client()
            req = (
                lark_bitable.UpdateAppTableRecordRequest.builder()
                .app_token(app_token)
                .table_id(table_id)
                .record_id(record_id)
                .request_body(self._build_record_request_body(fields))
                .build()
            )
            resp = client.bitable.v1.app_table_record.update(req)
            self._require_success(resp, "Update Bitable record")
            record = getattr(getattr(resp, "data", None), "record", None)
            return self._json(
                {
                    "ok": True,
                    "app_token": app_token,
                    "table_id": table_id,
                    "record_id": record_id,
                    "record": self._record_payload(record) if record else None,
                }
            )
        except Exception as e:
            return f"Error: {e}"


class FeishuBitableCreateAppTool(_FeishuBitableToolBase):
    @property
    def name(self) -> str:
        return "feishu_bitable_create_app"

    @property
    def description(self) -> str:
        return "Create a new Feishu Bitable app (multi-dimensional table app)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "App name."},
                "folder_token": {"type": "string", "description": "Optional folder token."},
                "time_zone": {"type": "string", "description": "Optional time zone, e.g. Asia/Shanghai."},
            },
            "required": ["name"],
        }

    async def execute(
        self,
        name: str,
        folder_token: str | None = None,
        time_zone: str | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            client = self._bitable_client()
            body_builder = lark_bitable.ReqApp.builder().name(name)
            if folder_token:
                body_builder = body_builder.folder_token(folder_token)
            if time_zone:
                body_builder = body_builder.time_zone(time_zone)
            req = lark_bitable.CreateAppRequest.builder().request_body(body_builder.build()).build()
            resp = client.bitable.v1.app.create(req)
            self._require_success(resp, "Create Bitable app")
            app = getattr(getattr(resp, "data", None), "app", None)
            return self._json(
                {
                    "ok": True,
                    "app": self._to_jsonable(app) if app else None,
                }
            )
        except Exception as e:
            return f"Error: {e}"


class FeishuBitableCreateFieldTool(_FeishuBitableToolBase):
    @property
    def name(self) -> str:
        return "feishu_bitable_create_field"

    @property
    def description(self) -> str:
        return "Create a new field (column) in a Feishu Bitable table."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "app_token": {"type": "string"},
                "table_id": {"type": "string"},
                "field_name": {"type": "string", "description": "Field (column) name."},
                "field_type": {
                    "type": "integer",
                    "description": "Bitable field type ID (for example 1=text, 2=number, 3=single select).",
                },
                "property": {
                    "type": "object",
                    "description": "Optional Bitable field property payload (type-specific config).",
                },
            },
            "required": ["app_token", "table_id", "field_name", "field_type"],
        }

    async def execute(
        self,
        app_token: str,
        table_id: str,
        field_name: str,
        field_type: int,
        property: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            client = self._bitable_client()
            body_builder = (
                lark_bitable.AppTableField.builder()
                .field_name(field_name)
                .type(field_type)
            )
            if property:
                body_builder = body_builder.property(property)
            req = (
                lark_bitable.CreateAppTableFieldRequest.builder()
                .app_token(app_token)
                .table_id(table_id)
                .client_token(str(uuid.uuid4()))
                .request_body(body_builder.build())
                .build()
            )
            resp = client.bitable.v1.app_table_field.create(req)
            self._require_success(resp, "Create Bitable field")
            field = getattr(getattr(resp, "data", None), "field", None)
            field_type_val = getattr(field, "type", None)
            return self._json(
                {
                    "ok": True,
                    "app_token": app_token,
                    "table_id": table_id,
                    "field": {
                        "field_id": getattr(field, "field_id", None),
                        "field_name": getattr(field, "field_name", None),
                        "type": field_type_val,
                        "type_name": _BITABLE_FIELD_TYPE_NAMES.get(field_type_val, f"type_{field_type_val}")
                        if field_type_val is not None
                        else None,
                        "property": getattr(field, "property", None),
                    },
                }
            )
        except Exception as e:
            return f"Error: {e}"
