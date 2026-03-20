import json
from types import SimpleNamespace

import pytest

from feibot.agent.tools.feishu import (
    FeishuAppScopesTool,
    FeishuBitableCreateFieldTool,
    FeishuBitableCreateRecordTool,
    FeishuBitableGetMetaTool,
    FeishuDocTool,
    FeishuDriveTool,
    FeishuPermTool,
    FeishuWikiTool,
)


@pytest.mark.asyncio
async def test_feishu_bitable_get_meta_parses_base_url():
    tool = FeishuBitableGetMetaTool()
    result = await tool.execute(
        url="https://example.feishu.cn/base/bascnABC123?table=tblXYZ789",
        fetch_tables=False,
    )
    data = json.loads(result)
    assert data["ok"] is True
    assert data["app_token"] == "bascnABC123"
    assert data["table_id"] == "tblXYZ789"
    assert data["is_wiki_url"] is False


@pytest.mark.asyncio
async def test_feishu_bitable_get_meta_reports_wiki_url_warning():
    tool = FeishuBitableGetMetaTool()
    result = await tool.execute(
        url="https://example.feishu.cn/wiki/ABCD1234",
        fetch_tables=False,
    )
    data = json.loads(result)
    assert data["ok"] is True
    assert data["is_wiki_url"] is True
    assert data["app_token"] is None
    assert any("Wiki-style URL" in msg for msg in data.get("warnings", []))


@pytest.mark.asyncio
async def test_feishu_doc_requires_credentials_for_create():
    tool = FeishuDocTool(app_id="", app_secret="")
    result = await tool.execute(action="create", title="Test Doc")
    assert "Error:" in result
    assert "credentials" in result.lower()


def test_feishu_doc_resolve_doc_token_from_docx_url():
    tool = FeishuDocTool()
    token = tool._resolve_doc_token(
        doc_token=None,
        url="https://acme.feishu.cn/docx/AbCdEfGhIjKlMnOpQrStUvWxYz1",
    )
    assert token == "AbCdEfGhIjKlMnOpQrStUvWxYz1"


def test_feishu_doc_resolve_doc_token_rejects_non_docx_url():
    tool = FeishuDocTool()
    with pytest.raises(ValueError):
        tool._resolve_doc_token(
            doc_token=None,
            url="https://acme.feishu.cn/wiki/UGdVwyrqJi0Qy9kRuuDcKmYynph",
        )

    with pytest.raises(RuntimeError, match="root block"):
        tool._delete_block(client=object(), doc_token="doccnROOT", block_id="doccnROOT")


@pytest.mark.asyncio
async def test_feishu_bitable_create_record_requires_credentials():
    tool = FeishuBitableCreateRecordTool(app_id="", app_secret="")
    result = await tool.execute(app_token="bascnABC", table_id="tblXYZ", fields={"Name": "Alice"})
    assert "Error:" in result
    assert "credentials" in result.lower()


@pytest.mark.asyncio
async def test_feishu_bitable_create_field_requires_credentials():
    tool = FeishuBitableCreateFieldTool(app_id="", app_secret="")
    result = await tool.execute(
        app_token="bascnABC",
        table_id="tblXYZ",
        field_name="Status",
        field_type=3,
    )
    assert "Error:" in result
    assert "credentials" in result.lower()


@pytest.mark.asyncio
async def test_feishu_drive_requires_credentials():
    tool = FeishuDriveTool(app_id="", app_secret="")
    result = await tool.execute(action="list")
    assert "Error:" in result
    assert "credentials" in result.lower()


def test_feishu_doc_create_includes_owner_admin_grant(monkeypatch):
    class _BodyBuilder:
        def __init__(self):
            self.payload = {}

        def title(self, value):
            self.payload["title"] = value
            return self

        def folder_token(self, value):
            self.payload["folder_token"] = value
            return self

        def build(self):
            return dict(self.payload)

    class _ReqBuilder:
        def __init__(self):
            self.payload = {}

        def request_body(self, body):
            self.payload["request_body"] = body
            return self

        def build(self):
            return dict(self.payload)

    fake_lark_docx = SimpleNamespace(
        CreateDocumentRequestBody=SimpleNamespace(builder=lambda: _BodyBuilder()),
        CreateDocumentRequest=SimpleNamespace(builder=lambda: _ReqBuilder()),
    )
    monkeypatch.setattr("feibot.agent.tools.feishu.lark_docx", fake_lark_docx)

    class _Resp:
        code = 0
        msg = "ok"

        def __init__(self):
            self.data = SimpleNamespace(
                document=SimpleNamespace(document_id="doccnTEST123", title="Test Doc", revision_id="1")
            )

        def success(self):
            return True

    fake_client = SimpleNamespace(
        docx=SimpleNamespace(
            v1=SimpleNamespace(
                document=SimpleNamespace(create=lambda _req: _Resp())
            )
        )
    )

    tool = FeishuDocTool(app_id="app", app_secret="secret", owner_open_id="ou_owner")
    monkeypatch.setattr(
        tool,
        "_grant_doc_admin_permission_best_effort",
        lambda **kwargs: {"status": "granted", "member_id": kwargs["owner_open_id"], "doc_type": "docx"},
    )

    data = json.loads(tool._create_doc(fake_client, title="Test Doc", folder_token=None))
    assert data["ok"] is True
    assert data["document"]["document_id"] == "doccnTEST123"
    assert data["owner_admin_grant"]["status"] == "granted"
    assert data["owner_admin_grant"]["member_id"] == "ou_owner"


def test_feishu_doc_create_uses_wiki_by_default_when_configured(monkeypatch):
    class _WikiResp:
        code = 0
        msg = "ok"

        def __init__(self):
            self.data = SimpleNamespace(
                node=SimpleNamespace(
                    space_id=777,
                    node_token="wikcnNODE123",
                    parent_node_token="wikcnPARENT",
                    obj_type="docx",
                    obj_token="doccnWIKI123",
                    title="Wiki Doc",
                )
            )

        def success(self):
            return True

    fake_client = SimpleNamespace(
        wiki=SimpleNamespace(
            v2=SimpleNamespace(
                space_node=SimpleNamespace(create=lambda _req: _WikiResp())
            )
        )
    )

    tool = FeishuDocTool(
        app_id="app",
        app_secret="secret",
        owner_open_id="ou_owner",
        wiki_space_id="777",
        wiki_parent_node_token="wikcnPARENT",
    )
    monkeypatch.setattr(
        tool,
        "_grant_doc_admin_permission_best_effort",
        lambda **kwargs: {"status": "granted", "member_id": kwargs["owner_open_id"]},
    )

    data = json.loads(tool._create_doc(fake_client, title="Wiki Doc", folder_token=None))
    assert data["ok"] is True
    assert data["created_via"] == "wiki"
    assert data["document"]["document_id"] == "doccnWIKI123"
    assert data["wiki_node"]["node_token"] == "wikcnNODE123"
    assert data["wiki_node"]["obj_token"] == "doccnWIKI123"
    assert data["owner_admin_grant"]["status"] == "granted"


def test_feishu_doc_create_uses_parent_node_to_resolve_space_id(monkeypatch):
    class _WikiResp:
        code = 0
        msg = "ok"

        def __init__(self):
            self.data = SimpleNamespace(
                node=SimpleNamespace(
                    space_id=888,
                    node_token="wikcnNODE888",
                    parent_node_token="JnYiwVERWiPzxQk6NbzcK6E3nje",
                    obj_type="docx",
                    obj_token="doccnWIKI888",
                    title="Wiki Parent Only",
                )
            )

        def success(self):
            return True

    fake_client = SimpleNamespace(
        wiki=SimpleNamespace(
            v2=SimpleNamespace(
                space_node=SimpleNamespace(create=lambda _req: _WikiResp())
            )
        )
    )
    tool = FeishuDocTool(
        app_id="app",
        app_secret="secret",
        wiki_parent_node_token="JnYiwVERWiPzxQk6NbzcK6E3nje",
    )
    monkeypatch.setattr(tool, "_resolve_wiki_space_id_from_node", lambda client, wiki_node_token: "888")
    monkeypatch.setattr(tool, "_grant_doc_admin_permission_best_effort", lambda **kwargs: None)

    data = json.loads(tool._create_doc(fake_client, title="Wiki Parent Only", folder_token=None))
    assert data["ok"] is True
    assert data["created_via"] == "wiki"
    assert data["document"]["document_id"] == "doccnWIKI888"
    assert data["wiki_node"]["space_id"] == "888"
    assert data["wiki_node"]["parent_node_token"] == "JnYiwVERWiPzxQk6NbzcK6E3nje"


def test_feishu_doc_clean_blocks_preserves_image_hierarchy(monkeypatch):
    tool = FeishuDocTool()
    monkeypatch.setattr(
        "feibot.agent.tools.feishu.lark_docx",
        SimpleNamespace(Block=lambda payload: payload),
    )

    image_block = {
        "block_type": 27,
        "block_id": "img_block_1",
        "parent_id": "paragraph_1",
        "children": [],
        "image": {"token": "placeholder"},
        "comment_ids": ["c1"],
    }
    paragraph_block = {
        "block_type": 2,
        "block_id": "paragraph_1",
        "parent_id": "doc_1",
        "children": ["img_block_1"],
        "text": {"elements": []},
    }

    cleaned, skipped = tool._clean_blocks_for_insert([paragraph_block, image_block])

    assert skipped == []
    assert len(cleaned) == 2
    assert cleaned[0]["block_id"] == "paragraph_1"
    assert cleaned[0]["children"] == ["img_block_1"]
    assert cleaned[1]["block_id"] == "img_block_1"
    assert cleaned[1]["parent_id"] == "paragraph_1"
    assert "comment_ids" not in cleaned[1]


def test_feishu_doc_clean_blocks_keeps_table_and_table_cell(monkeypatch):
    tool = FeishuDocTool()
    monkeypatch.setattr(
        "feibot.agent.tools.feishu.lark_docx",
        SimpleNamespace(Block=lambda payload: payload),
    )

    table_block = {
        "block_type": 31,
        "block_id": "tbl_1",
        "parent_id": "doc_1",
        "children": ["cell_1"],
        "table": {"rows_size": 1, "columns_size": 1, "merge_info": {"x": 1}},
    }
    table_cell_block = {
        "block_type": 32,
        "block_id": "cell_1",
        "parent_id": "tbl_1",
        "children": [],
    }

    cleaned, skipped = tool._clean_blocks_for_insert([table_block, table_cell_block])

    assert skipped == []
    assert len(cleaned) == 2
    assert cleaned[0]["block_type"] == 31
    assert "merge_info" not in cleaned[0]["table"]
    assert cleaned[1]["block_type"] == 32


def test_feishu_doc_split_markdown_preserves_table_atomic_block():
    tool = FeishuDocTool()
    markdown = (
        "# Title\n\n"
        "Intro paragraph.\n\n"
        "| Col A | Col B |\n"
        "| --- | --- |\n"
        "| Value 1 | Value 2 |\n"
        "| Value 3 | Value 4 |\n"
    )

    chunks = tool._split_markdown_for_docx(markdown, max_chars=40)

    table_chunks = [c for c in chunks if "| Col A | Col B |" in c]
    assert len(table_chunks) == 1
    assert "| --- | --- |" in table_chunks[0]
    assert "| Value 3 | Value 4 |" in table_chunks[0]


def test_feishu_wiki_resolve_token_from_url():
    tool = FeishuWikiTool()
    token = tool._resolve_wiki_node_token(
        token=None,
        url="https://acnemqx5swew.feishu.cn/wiki/JnYiwVERWiPzxQk6NbzcK6E3nje",
    )
    assert token == "JnYiwVERWiPzxQk6NbzcK6E3nje"


@pytest.mark.asyncio
async def test_feishu_wiki_requires_credentials():
    tool = FeishuWikiTool(app_id="", app_secret="")
    result = await tool.execute(action="spaces")
    assert "Error:" in result
    assert "credentials" in result.lower()


@pytest.mark.asyncio
async def test_feishu_wiki_spaces_default_page_size_is_50(monkeypatch):
    tool = FeishuWikiTool(app_id="app", app_secret="secret")
    captured: dict[str, object] = {}

    monkeypatch.setattr(tool, "_create_client", lambda: object())

    def _fake_list_spaces(client, page_size, page_token):
        captured["page_size"] = page_size
        captured["page_token"] = page_token
        return json.dumps({"ok": True, "action": "spaces"})

    monkeypatch.setattr(tool, "_list_spaces", _fake_list_spaces)

    result = await tool.execute(action="spaces")
    data = json.loads(result)
    assert data["ok"] is True
    assert captured["page_size"] == 50
    assert captured["page_token"] is None


@pytest.mark.asyncio
async def test_feishu_wiki_nodes_casts_integer_space_id(monkeypatch):
    tool = FeishuWikiTool(app_id="app", app_secret="secret")
    captured: dict[str, object] = {}

    monkeypatch.setattr(tool, "_create_client", lambda: object())

    def _fake_list_nodes(client, space_id, parent_node_token, page_size, page_token):
        captured["space_id"] = space_id
        captured["parent_node_token"] = parent_node_token
        captured["page_size"] = page_size
        captured["page_token"] = page_token
        return json.dumps({"ok": True, "action": "nodes", "space_id": space_id})

    monkeypatch.setattr(tool, "_list_nodes", _fake_list_nodes)

    result = await tool.execute(action="nodes", space_id=7610642581256440761)
    data = json.loads(result)
    assert data["ok"] is True
    assert captured["space_id"] == "7610642581256440761"
    assert captured["page_size"] == 50
    assert captured["parent_node_token"] is None
    assert captured["page_token"] is None


@pytest.mark.asyncio
async def test_feishu_app_scopes_requires_credentials():
    tool = FeishuAppScopesTool(app_id="", app_secret="")
    result = await tool.execute()
    assert "Error:" in result
    assert "credentials" in result.lower()


@pytest.mark.asyncio
async def test_feishu_perm_requires_credentials():
    tool = FeishuPermTool(app_id="", app_secret="")
    result = await tool.execute(action="list", token="doccnX", type="docx")
    assert "Error:" in result
    assert "credentials" in result.lower()


def test_feishu_doc_write_sorts_blocks_by_first_level(monkeypatch):
    tool = FeishuDocTool()
    fake_client = object()

    blocks = [
        SimpleNamespace(block_id="b2", block_type=2),
        SimpleNamespace(block_id="b1", block_type=2),
        SimpleNamespace(block_id="c1", block_type=2),
    ]
    seen_order: list[str] = []

    monkeypatch.setattr(tool, "_convert_markdown_to_blocks", lambda client, markdown: (blocks, ["b1", "b2"]))

    def _clean(in_blocks):
        seen_order.extend(str(getattr(b, "block_id", "")) for b in in_blocks)
        return in_blocks, []

    monkeypatch.setattr(tool, "_clean_blocks_for_insert", _clean)
    monkeypatch.setattr(
        tool,
        "_insert_blocks",
        lambda client, doc_token, blocks: [SimpleNamespace(block_id="i1"), SimpleNamespace(block_id="i2")],
    )

    data = json.loads(tool._write_or_append_doc(fake_client, "doccn123", "# title", replace=False))
    assert data["ok"] is True
    assert data["action"] == "append"
    assert seen_order == ["b1", "b2", "c1"]


@pytest.mark.asyncio
async def test_feishu_doc_execute_write_safe_routes_with_force_chunked(monkeypatch):
    tool = FeishuDocTool(app_id="app", app_secret="secret")
    fake_client = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(tool, "_create_client", lambda: fake_client)
    monkeypatch.setattr(tool, "_resolve_doc_token", lambda **kwargs: "doccnSAFE123")

    def _fake_write_or_append(
        client,
        doc_token,
        markdown,
        replace,
        *,
        force_chunked=False,
        chunk_chars=None,
    ):
        captured["client"] = client
        captured["doc_token"] = doc_token
        captured["markdown"] = markdown
        captured["replace"] = replace
        captured["force_chunked"] = force_chunked
        captured["chunk_chars"] = chunk_chars
        return json.dumps({"ok": True, "action": "write"})

    monkeypatch.setattr(tool, "_write_or_append_doc", _fake_write_or_append)

    result = await tool.execute(
        action="write_safe",
        doc_token="doccnSAFE123",
        content="## section\nhello",
        chunk_chars=2600,
    )
    data = json.loads(result)
    assert data["ok"] is True
    assert captured["doc_token"] == "doccnSAFE123"
    assert captured["replace"] is True
    assert captured["force_chunked"] is True
    assert captured["chunk_chars"] == 2600


def test_feishu_doc_write_falls_back_to_chunked_when_single_batch_fails(monkeypatch):
    tool = FeishuDocTool()
    fake_client = object()

    monkeypatch.setattr(
        tool,
        "_convert_markdown_to_blocks",
        lambda client, markdown: ([SimpleNamespace(block_id="b1", block_type=2)], ["b1"]),
    )
    monkeypatch.setattr(tool, "_clean_blocks_for_insert", lambda blocks: (blocks, []))
    monkeypatch.setattr(
        tool,
        "_insert_blocks_with_retry",
        lambda client, doc_token, blocks: (_ for _ in ()).throw(
            RuntimeError("Insert DocX blocks failed: code=99992402")
        ),
    )
    monkeypatch.setattr(
        tool,
        "_insert_markdown_chunks",
        lambda client, doc_token, markdown, chunk_chars: {
            "chunk_chars": chunk_chars,
            "chunk_count": 2,
            "successful_chunks": 2,
            "failed_chunks": 0,
            "converted_blocks": 3,
            "inserted_blocks": 3,
            "skipped_block_types": [],
            "failed_chunk_details": [],
        },
    )

    data = json.loads(tool._write_or_append_doc(fake_client, "doccn123", "# title", replace=False))
    assert data["ok"] is True
    assert data["strategy"] == "chunked_fallback"
    assert data["inserted_blocks"] == 3
    assert "code=99992402" in data["fallback_from_error"]


@pytest.mark.asyncio
async def test_feishu_doc_execute_write_auto_forces_chunked_when_content_large(monkeypatch):
    tool = FeishuDocTool(
        app_id="app",
        app_secret="secret",
        auto_chunk_threshold_chars=10,
    )
    fake_client = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(tool, "_create_client", lambda: fake_client)
    monkeypatch.setattr(tool, "_resolve_doc_token", lambda **kwargs: "doccnAUTO123")

    def _fake_write_or_append(
        client,
        doc_token,
        markdown,
        replace,
        *,
        force_chunked=False,
        chunk_chars=None,
    ):
        captured["force_chunked"] = force_chunked
        captured["replace"] = replace
        captured["markdown"] = markdown
        captured["chunk_chars"] = chunk_chars
        return json.dumps({"ok": True, "action": "write", "strategy": "chunked_forced"})

    monkeypatch.setattr(tool, "_write_or_append_doc", _fake_write_or_append)

    result = await tool.execute(
        action="write",
        doc_token="doccnAUTO123",
        content="0123456789abcdef",
    )
    data = json.loads(result)
    assert data["ok"] is True
    assert captured["replace"] is True
    assert captured["force_chunked"] is True


@pytest.mark.asyncio
async def test_feishu_doc_insert_image_action_routes_to_helper(monkeypatch):
    tool = FeishuDocTool(app_id="app", app_secret="secret")
    fake_client = object()
    monkeypatch.setattr(tool, "_create_client", lambda: fake_client)
    monkeypatch.setattr(tool, "_resolve_doc_token", lambda **kwargs: "doccnIMG123")
    monkeypatch.setattr(
        tool,
        "_insert_image_into_doc",
        lambda client, doc_token, image_path, width=None, height=None, scale=None: json.dumps(
            {
                "ok": True,
                "action": "insert_image",
                "doc_token": doc_token,
                "image_path": image_path,
                "width": width,
                "height": height,
                "scale": scale,
            }
        ),
    )

    result = await tool.execute(
        action="insert_image",
        doc_token="doccnIMG123",
        image_path="/tmp/example.png",
        image_width=640,
        image_height=480,
        image_scale=1.0,
    )
    data = json.loads(result)
    assert data["ok"] is True
    assert data["action"] == "insert_image"
    assert data["doc_token"] == "doccnIMG123"
    assert data["image_path"] == "/tmp/example.png"
