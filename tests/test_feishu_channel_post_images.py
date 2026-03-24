import asyncio
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from feibot.bus.events import OutboundMessage
from feibot.bus.queue import MessageBus
from feibot.channels.feishu import FeishuChannel
from feibot.config.schema import FeishuConfig


def _make_channel(tmp_path: Path) -> FeishuChannel:
    return FeishuChannel(
        config=FeishuConfig(enabled=True, app_id="app", app_secret="secret"),
        bus=MessageBus(),
        workspace_dir=tmp_path,
    )


class _ReqBodyBuilder:
    def __init__(self):
        self.payload: dict[str, object] = {}

    def receive_id(self, value: str):
        self.payload["receive_id"] = value
        return self

    def msg_type(self, value: str):
        self.payload["msg_type"] = value
        return self

    def content(self, value: str):
        self.payload["content"] = value
        return self

    def build(self):
        return dict(self.payload)


class _ReqBuilder:
    def __init__(self):
        self.payload: dict[str, object] = {}

    def receive_id_type(self, value: str):
        self.payload["receive_id_type"] = value
        return self

    def request_body(self, value: dict[str, object]):
        self.payload["request_body"] = value
        return self

    def build(self):
        return dict(self.payload)


class _SendResponse:
    def __init__(self, ok: bool, *, code: int = 0, msg: str = "", log_id: str = ""):
        self._ok = ok
        self.code = code
        self.msg = msg
        self._log_id = log_id

    def success(self) -> bool:
        return self._ok

    def get_log_id(self) -> str:
        return self._log_id


def _markdown_tables(count: int) -> str:
    blocks: list[str] = []
    for idx in range(count):
        blocks.append(
            (
                f"### Table {idx + 1}\n"
                "| Col A | Col B |\n"
                "| --- | --- |\n"
                f"| Value {idx} | Data {idx} |"
            )
        )
    return "\n\n".join(blocks)


def test_extract_post_content_collects_text_and_image_keys(tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    raw = json.dumps(
        {
            "title": "Daily",
            "content": [
                [
                    {"tag": "text", "text": "report"},
                    {"tag": "img", "image_key": "img_1"},
                    {"tag": "img", "image_key": "img_2"},
                ]
            ],
        },
        ensure_ascii=False,
    )

    text, image_keys = channel._extract_post_content(raw)

    assert text == "Daily\nreport"
    assert image_keys == ["img_1", "img_2"]


def test_merge_quoted_message_formats_quote_block(tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)

    merged = channel._merge_quoted_message("follow-up question", "line-1\nline-2")

    assert merged.startswith("[Quoted message]")
    assert "> line-1" in merged
    assert "> line-2" in merged
    assert "follow-up question" in merged


def test_parse_interactive_content_extracts_text_fragments(tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    raw = json.dumps(
        {
            "title": "Card title",
            "elements": [
                [
                    {"tag": "img", "image_key": "img_1"},
                    {"tag": "text", "text": "please update your client"},
                ]
            ],
        },
        ensure_ascii=False,
    )

    parsed = channel._parse_message_content("interactive", raw)

    assert "Card title" in parsed
    assert "please update your client" in parsed


def test_build_merge_forward_preview_uses_nested_items(tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    items = [
        SimpleNamespace(
            msg_type="merge_forward",
            body=SimpleNamespace(content="Merged and Forwarded Message"),
        ),
        SimpleNamespace(
            msg_type="text",
            body=SimpleNamespace(content=json.dumps({"text": "first forwarded note"}, ensure_ascii=False)),
        ),
        SimpleNamespace(
            msg_type="interactive",
            body=SimpleNamespace(
                content=json.dumps(
                    {
                        "title": "Card",
                        "elements": [[{"tag": "text", "text": "please update your client"}]],
                    },
                    ensure_ascii=False,
                )
            ),
        ),
    ]

    preview = channel._build_merge_forward_preview(items)

    assert preview.startswith("[Merged forward history]")
    assert "first forwarded note" in preview
    assert "please update your client" in preview


@pytest.mark.asyncio
async def test_on_message_downloads_images_embedded_in_post(monkeypatch, tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)

    async def _noop_reaction(*args, **kwargs):
        return None

    seen_keys: list[str] = []

    async def _fake_download(message_id: str, msg_type: str, raw_content: str) -> str | None:
        payload = json.loads(raw_content)
        key = payload.get("image_key")
        seen_keys.append(key)
        return f"/tmp/{key}.jpg"

    captured: dict[str, object] = {}

    async def _fake_handle_message(sender_id: str, chat_id: str, content: str, media: list[str], metadata: dict):
        captured["sender_id"] = sender_id
        captured["chat_id"] = chat_id
        captured["content"] = content
        captured["media"] = media
        captured["metadata"] = metadata

    monkeypatch.setattr(channel, "_add_reaction", _noop_reaction)
    monkeypatch.setattr(channel, "_download_message_resource", _fake_download)
    monkeypatch.setattr(channel, "_handle_message", _fake_handle_message)

    post_payload = {
        "title": "Title",
        "content": [
            [
                {"tag": "text", "text": "hello"},
                {"tag": "img", "image_key": "img_a"},
            ]
        ],
    }
    data = SimpleNamespace(
        event=SimpleNamespace(
            message=SimpleNamespace(
                message_id="om_1",
                chat_id="oc_group_1",
                chat_type="group",
                message_type="post",
                content=json.dumps(post_payload, ensure_ascii=False),
            ),
            sender=SimpleNamespace(
                sender_type="user",
                sender_id=SimpleNamespace(open_id="ou_1"),
            ),
        )
    )

    await channel._on_message(data)

    assert seen_keys == ["img_a"]
    assert captured["chat_id"] == "oc_group_1"
    assert captured["media"] == ["/tmp/img_a.jpg"]
    assert "Title" in str(captured["content"])
    assert "hello" in str(captured["content"])


@pytest.mark.asyncio
async def test_on_message_includes_parent_quote_and_metadata(monkeypatch, tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)

    async def _noop_reaction(*args, **kwargs):
        return None

    async def _fake_fetch_quote(parent_message_id: str) -> tuple[str | None, str | None]:
        assert parent_message_id == "om_parent_1"
        return "original context", "text"

    captured: dict[str, object] = {}

    async def _fake_handle_message(sender_id: str, chat_id: str, content: str, media: list[str], metadata: dict):
        captured["sender_id"] = sender_id
        captured["chat_id"] = chat_id
        captured["content"] = content
        captured["media"] = media
        captured["metadata"] = metadata

    monkeypatch.setattr(channel, "_add_reaction", _noop_reaction)
    monkeypatch.setattr(channel, "_fetch_quoted_message", _fake_fetch_quote)
    monkeypatch.setattr(channel, "_handle_message", _fake_handle_message)

    data = SimpleNamespace(
        event=SimpleNamespace(
            message=SimpleNamespace(
                message_id="om_quoted_1",
                parent_id="om_parent_1",
                root_id="om_root_1",
                thread_id="omt_1",
                chat_id="oc_group_1",
                chat_type="group",
                message_type="text",
                content=json.dumps({"text": "please continue"}, ensure_ascii=False),
            ),
            sender=SimpleNamespace(
                sender_type="user",
                sender_id=SimpleNamespace(open_id="ou_1"),
            ),
        )
    )

    await channel._on_message(data)

    assert captured["chat_id"] == "oc_group_1"
    assert captured["media"] == []
    assert "[Quoted message]" in str(captured["content"])
    assert "original context" in str(captured["content"])
    assert "please continue" in str(captured["content"])
    assert captured["metadata"] == {
        "message_id": "om_quoted_1",
        "chat_type": "group",
        "msg_type": "text",
        "parent_id": "om_parent_1",
        "root_id": "om_root_1",
        "thread_id": "omt_1",
        "quoted_msg_type": "text",
    }


@pytest.mark.asyncio
async def test_on_message_quote_only_text_is_forwarded(monkeypatch, tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)

    async def _noop_reaction(*args, **kwargs):
        return None

    async def _fake_fetch_quote(parent_message_id: str) -> tuple[str | None, str | None]:
        assert parent_message_id == "om_parent_2"
        return "quoted only context", "text"

    captured: dict[str, object] = {}

    async def _fake_handle_message(sender_id: str, chat_id: str, content: str, media: list[str], metadata: dict):
        captured["content"] = content
        captured["metadata"] = metadata

    monkeypatch.setattr(channel, "_add_reaction", _noop_reaction)
    monkeypatch.setattr(channel, "_fetch_quoted_message", _fake_fetch_quote)
    monkeypatch.setattr(channel, "_handle_message", _fake_handle_message)

    data = SimpleNamespace(
        event=SimpleNamespace(
            message=SimpleNamespace(
                message_id="om_quoted_2",
                parent_id="om_parent_2",
                chat_id="oc_group_2",
                chat_type="group",
                message_type="text",
                content=json.dumps({"text": ""}, ensure_ascii=False),
            ),
            sender=SimpleNamespace(
                sender_type="user",
                sender_id=SimpleNamespace(open_id="ou_2"),
            ),
        )
    )

    await channel._on_message(data)

    assert "[Quoted message]" in str(captured["content"])
    assert "quoted only context" in str(captured["content"])
    assert captured["metadata"]["parent_id"] == "om_parent_2"


@pytest.mark.asyncio
async def test_on_message_quote_only_with_fetch_failure_uses_placeholder(monkeypatch, tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)

    async def _noop_reaction(*args, **kwargs):
        return None

    async def _fake_fetch_quote(parent_message_id: str) -> tuple[str | None, str | None]:
        assert parent_message_id == "om_parent_3"
        return None, None

    captured: dict[str, object] = {}

    async def _fake_handle_message(sender_id: str, chat_id: str, content: str, media: list[str], metadata: dict):
        captured["content"] = content
        captured["metadata"] = metadata

    monkeypatch.setattr(channel, "_add_reaction", _noop_reaction)
    monkeypatch.setattr(channel, "_fetch_quoted_message", _fake_fetch_quote)
    monkeypatch.setattr(channel, "_handle_message", _fake_handle_message)

    data = SimpleNamespace(
        event=SimpleNamespace(
            message=SimpleNamespace(
                message_id="om_quoted_3",
                parent_id="om_parent_3",
                chat_id="oc_group_3",
                chat_type="group",
                message_type="text",
                content=json.dumps({"text": ""}, ensure_ascii=False),
            ),
            sender=SimpleNamespace(
                sender_type="user",
                sender_id=SimpleNamespace(open_id="ou_3"),
            ),
        )
    )

    await channel._on_message(data)

    assert captured["content"] == "[Quoted message: om_parent_3]"
    assert captured["metadata"]["parent_id"] == "om_parent_3"


def test_download_message_resource_maps_audio_type_to_file(monkeypatch, tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    seen: dict[str, str] = {}

    class _ReqBuilder:
        def __init__(self):
            self.payload: dict[str, str] = {}

        def message_id(self, value: str):
            self.payload["message_id"] = value
            return self

        def file_key(self, value: str):
            self.payload["file_key"] = value
            return self

        def type(self, value: str):
            self.payload["type"] = value
            return self

        def build(self):
            return dict(self.payload)

    class _Resp:
        file_name = "voice.m4a"

        @staticmethod
        def success() -> bool:
            return True

        @property
        def file(self):
            return SimpleNamespace(read=lambda: b"audio-bytes")

    def _fake_get(request: dict) -> _Resp:
        seen["type"] = request["type"]
        return _Resp()

    monkeypatch.setattr(
        "feibot.channels.feishu.GetMessageResourceRequest",
        SimpleNamespace(builder=lambda: _ReqBuilder()),
    )
    channel._client = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(
                message_resource=SimpleNamespace(get=_fake_get),
            )
        )
    )

    out = channel._download_message_resource_sync(
        message_id="om_audio_1",
        msg_type="audio",
        resource_key="file_key_1",
        file_name_hint="voice.m4a",
    )

    assert seen["type"] == "file"
    assert out.endswith(".m4a")


@pytest.mark.asyncio
async def test_on_card_action_sync_routes_to_approve_command(monkeypatch, tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    captured: dict[str, object] = {}

    async def _fake_handle_message(sender_id: str, chat_id: str, content: str, media=None, metadata=None):
        captured["sender_id"] = sender_id
        captured["chat_id"] = chat_id
        captured["content"] = content
        captured["metadata"] = metadata or {}

    monkeypatch.setattr(channel, "_handle_message", _fake_handle_message)
    channel._loop = asyncio.get_running_loop()

    data = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(
                value={
                    "type": "exec_approval",
                    "approval_id": "abc123",
                    "decision": "allow-once",
                    "command_preview": "rm ./tmp/file.txt",
                    "working_dir": "/workspace/feibot",
                    "risk_level": "dangerous",
                }
            ),
            operator=SimpleNamespace(open_id="ou_1", user_id=""),
            context=SimpleNamespace(open_chat_id="oc_1", open_message_id="om_card_1"),
        )
    )

    response = channel._on_card_action_sync(data)
    await asyncio.sleep(0.05)

    assert response is not None
    assert getattr(getattr(response, "card", None), "type", None) == "raw"
    card_text = str(getattr(getattr(response, "card", None), "data", {}))
    assert "read-only" in card_text
    assert "Allowed once" in card_text
    assert "rm ./tmp/file.txt" in card_text
    assert "dangerous" in card_text
    assert captured["sender_id"] == "ou_1"
    assert captured["chat_id"] == "oc_1"
    assert captured["content"] == "/approve abc123 allow-once"
    assert captured["metadata"]["approval_id"] == "abc123"
    assert captured["metadata"]["approval_decision"] == "allow-once"
    assert captured["metadata"]["source"] == "card_action"


@pytest.mark.asyncio
async def test_send_falls_back_to_plain_text_when_interactive_send_fails(monkeypatch, tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    calls: list[dict[str, object]] = []

    def _fake_create(request: dict[str, object]) -> _SendResponse:
        calls.append(request)
        if len(calls) == 1:
            return _SendResponse(False, code=230001, msg="invalid card", log_id="log_card_1")
        return _SendResponse(True, code=0, msg="ok", log_id="log_text_1")

    monkeypatch.setattr(
        "feibot.channels.feishu.CreateMessageRequestBody",
        SimpleNamespace(builder=lambda: _ReqBodyBuilder()),
    )
    monkeypatch.setattr(
        "feibot.channels.feishu.CreateMessageRequest",
        SimpleNamespace(builder=lambda: _ReqBuilder()),
    )
    channel._client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(create=_fake_create)))
    )

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="oc_group_1",
            content="final answer from bot",
        )
    )

    assert len(calls) == 2
    first_body = calls[0]["request_body"]
    second_body = calls[1]["request_body"]
    assert isinstance(first_body, dict)
    assert isinstance(second_body, dict)
    assert first_body["msg_type"] == "interactive"
    assert second_body["msg_type"] == "text"
    payload = json.loads(str(second_body["content"]))
    text = str(payload["text"])
    assert "Delivery warning" in text
    assert "code=230001" in text
    assert "final answer from bot" in text


@pytest.mark.asyncio
async def test_send_falls_back_to_plain_text_when_interactive_send_raises(monkeypatch, tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    calls: list[dict[str, object]] = []

    def _fake_create(request: dict[str, object]) -> _SendResponse:
        calls.append(request)
        if len(calls) == 1:
            raise RuntimeError("network timeout")
        return _SendResponse(True, code=0, msg="ok", log_id="log_text_2")

    monkeypatch.setattr(
        "feibot.channels.feishu.CreateMessageRequestBody",
        SimpleNamespace(builder=lambda: _ReqBodyBuilder()),
    )
    monkeypatch.setattr(
        "feibot.channels.feishu.CreateMessageRequest",
        SimpleNamespace(builder=lambda: _ReqBuilder()),
    )
    channel._client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(create=_fake_create)))
    )

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user_1",
            content="answer after tool call",
        )
    )

    assert len(calls) == 2
    first_body = calls[0]["request_body"]
    second_body = calls[1]["request_body"]
    assert isinstance(first_body, dict)
    assert isinstance(second_body, dict)
    assert first_body["msg_type"] == "interactive"
    assert second_body["msg_type"] == "text"
    payload = json.loads(str(second_body["content"]))
    text = str(payload["text"])
    assert "Delivery warning" in text
    assert "network timeout" in text


@pytest.mark.asyncio
async def test_send_falls_back_to_plain_text_when_post_send_fails(monkeypatch, tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    calls: list[dict[str, object]] = []

    def _fake_create(request: dict[str, object]) -> _SendResponse:
        calls.append(request)
        if len(calls) == 1:
            return _SendResponse(False, code=230099, msg="post payload invalid", log_id="log_post_1")
        return _SendResponse(True, code=0, msg="ok", log_id="log_text_3")

    monkeypatch.setattr(
        "feibot.channels.feishu.CreateMessageRequestBody",
        SimpleNamespace(builder=lambda: _ReqBodyBuilder()),
    )
    monkeypatch.setattr(
        "feibot.channels.feishu.CreateMessageRequest",
        SimpleNamespace(builder=lambda: _ReqBuilder()),
    )
    channel._client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(create=_fake_create)))
    )

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="oc_group_1",
            content="line1\nline2",
            metadata={"_feishu_msg_type": "post"},
        )
    )

    assert len(calls) == 2
    first_body = calls[0]["request_body"]
    second_body = calls[1]["request_body"]
    assert isinstance(first_body, dict)
    assert isinstance(second_body, dict)
    assert first_body["msg_type"] == "post"
    assert second_body["msg_type"] == "text"
    post_payload = json.loads(str(first_body["content"]))
    assert isinstance(post_payload.get("zh_cn"), dict)
    payload = json.loads(str(second_body["content"]))
    text = str(payload["text"])
    assert "Delivery warning" in text
    assert "code=230099" in text


def test_should_prefer_markdown_file_for_many_tables(tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)

    prefer, content_len, table_count = channel._should_prefer_markdown_file(_markdown_tables(5))

    assert prefer is True
    assert content_len > 0
    assert table_count == 5


@pytest.mark.asyncio
async def test_send_prefers_markdown_file_for_5_plus_tables(monkeypatch, tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    called: dict[str, object] = {}

    async def _fake_send_markdown_file_message(**kwargs):
        called.update(kwargs)
        return True, ""

    monkeypatch.setattr(channel, "_send_markdown_file_message", _fake_send_markdown_file_message)
    channel._client = object()

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="oc_group_1",
            content=_markdown_tables(5),
        )
    )

    assert called["receive_id_type"] == "chat_id"
    assert called["receive_id"] == "oc_group_1"
    assert "Table 1" in str(called["content"])


@pytest.mark.asyncio
async def test_send_markdown_file_failure_falls_back_to_text_without_interactive(
    monkeypatch,
    tmp_path: Path,
) -> None:
    channel = _make_channel(tmp_path)
    calls: list[dict[str, object]] = []

    async def _fake_send_markdown_file_message(**kwargs):
        return False, "upload failed"

    def _fake_create(request: dict[str, object]) -> _SendResponse:
        calls.append(request)
        return _SendResponse(True, code=0, msg="ok", log_id="log_text_4")

    monkeypatch.setattr(channel, "_send_markdown_file_message", _fake_send_markdown_file_message)
    monkeypatch.setattr(
        "feibot.channels.feishu.CreateMessageRequestBody",
        SimpleNamespace(builder=lambda: _ReqBodyBuilder()),
    )
    monkeypatch.setattr(
        "feibot.channels.feishu.CreateMessageRequest",
        SimpleNamespace(builder=lambda: _ReqBuilder()),
    )
    channel._client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(create=_fake_create)))
    )

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="oc_group_1",
            content=_markdown_tables(5),
        )
    )

    assert len(calls) == 1
    body = calls[0]["request_body"]
    assert isinstance(body, dict)
    assert body["msg_type"] == "text"
    payload = json.loads(str(body["content"]))
    text = str(payload["text"])
    assert "Delivery warning" in text
    assert "markdown file send failed" in text
    assert "upload failed" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"_tool_hint": True},
        {"_progress": True},
        {"_progress": True, "_tool_hint": True},
    ],
)
async def test_send_progress_or_tool_hint_never_uses_markdown_file(
    monkeypatch,
    tmp_path: Path,
    metadata: dict[str, object],
) -> None:
    channel = _make_channel(tmp_path)
    calls: list[dict[str, object]] = []
    markdown_calls = 0

    async def _fake_send_markdown_file_message(**kwargs):
        nonlocal markdown_calls
        markdown_calls += 1
        return True, ""

    def _fake_create(request: dict[str, object]) -> _SendResponse:
        calls.append(request)
        return _SendResponse(True, code=0, msg="ok", log_id="log_interactive_progress_1")

    monkeypatch.setattr(channel, "_send_markdown_file_message", _fake_send_markdown_file_message)
    monkeypatch.setattr(
        "feibot.channels.feishu.CreateMessageRequestBody",
        SimpleNamespace(builder=lambda: _ReqBodyBuilder()),
    )
    monkeypatch.setattr(
        "feibot.channels.feishu.CreateMessageRequest",
        SimpleNamespace(builder=lambda: _ReqBuilder()),
    )
    channel._client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(create=_fake_create)))
    )

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="oc_group_1",
            content=_markdown_tables(5),
            metadata=metadata,
        )
    )

    assert markdown_calls == 0
    assert len(calls) == 1
    body = calls[0]["request_body"]
    assert isinstance(body, dict)
    assert body["msg_type"] == "interactive"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"_tool_hint": True},
        {"_progress": True},
        {"_progress": True, "_tool_hint": True},
    ],
)
async def test_send_progress_or_tool_hint_truncates_to_3200_chars(
    monkeypatch,
    tmp_path: Path,
    metadata: dict[str, object],
) -> None:
    channel = _make_channel(tmp_path)
    calls: list[dict[str, object]] = []

    def _fake_create(request: dict[str, object]) -> _SendResponse:
        calls.append(request)
        return _SendResponse(True, code=0, msg="ok", log_id="log_interactive_progress_2")

    monkeypatch.setattr(
        "feibot.channels.feishu.CreateMessageRequestBody",
        SimpleNamespace(builder=lambda: _ReqBodyBuilder()),
    )
    monkeypatch.setattr(
        "feibot.channels.feishu.CreateMessageRequest",
        SimpleNamespace(builder=lambda: _ReqBuilder()),
    )
    channel._client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(create=_fake_create)))
    )

    long_text = "x" * 4000
    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="oc_group_1",
            content=long_text,
            metadata=metadata,
        )
    )

    assert len(calls) == 1
    body = calls[0]["request_body"]
    assert isinstance(body, dict)
    assert body["msg_type"] == "interactive"

    payload = json.loads(str(body["content"]))
    assert isinstance(payload, dict)
    card_body = payload.get("body")
    assert isinstance(card_body, dict)
    elements = card_body.get("elements")
    assert isinstance(elements, list) and elements
    first = elements[0]
    assert isinstance(first, dict)
    sent_text = str(first.get("content") or "")
    assert len(sent_text) == 3200
    assert sent_text == long_text[:3200]
