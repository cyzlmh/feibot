---
name: feishu-doc
description: "Operate Feishu DocX documents via the `feishu_doc` tool (create/read/list blocks/append/write/write_safe/get/update/delete block/insert_image). Use when the user wants to read or modify Feishu docs."
metadata: {"feibot":{"emoji":"📄"}}
---

# Feishu Doc Skill

Use `feishu_doc` for Feishu DocX operations, including inserting local images into a document.

## Workflow

1. If the user wants a new document, call `feishu_doc` with `action=create` and only `title` unless they explicitly provide valid tokens.
2. Prefer configured defaults for knowledge-base placement. Do not invent `wiki_space_id`, `wiki_parent_node_token`, or `folder_token`.
3. **Important**: `write_safe` requires an existing `doc_token`. For new documents, always `create` first, then use `write_safe` to populate content.
4. For full-document edits, prefer `write_safe` (single tool call with internal chunk+retry) when content is long or failure-prone; use `append`/`write` for shorter updates.
5. For precise edits, call `list_blocks` first, then `get_block`, then `update_block` or `delete_block`.
6. For Wiki links (`/wiki/...`), use `feishu_wiki` to resolve node details and get the underlying `obj_token` (DocX token) when needed.
7. To insert an image into a DocX document, use `feishu_doc` `action=insert_image` with a local `image_path`.

## Tool Call Examples

Create a doc (uses configured Wiki defaults when available):

```json
{
  "action": "create",
  "title": "Weekly Notes"
}
```

Read a doc:

```json
{
  "action": "read",
  "url": "https://example.feishu.cn/docx/AbCdEf123"
}
```

Append markdown:

```json
{
  "action": "append",
  "doc_token": "AbCdEf123",
  "content": "## Update\\n- Item A\\n- Item B"
}
```

Safely replace a full document with internal chunking:

```json
{
  "action": "write_safe",
  "doc_token": "AbCdEf123",
  "content": "## Full Report\\n...large markdown...",
  "chunk_chars": 3000
}
```

Insert a cached/local image into a DocX document:

```json
{
  "action": "insert_image",
  "doc_token": "AbCdEf123",
  "image_path": "/absolute/path/to/image.jpg"
}
```

Targeted block edit:

```json
{
  "action": "list_blocks",
  "doc_token": "AbCdEf123"
}
```

```json
{
  "action": "update_block",
  "doc_token": "AbCdEf123",
  "block_id": "doxcnBlockId",
  "content": "Rewritten line"
}
```

## Troubleshooting

- If credentials are missing, configure `channels.feishu.app_id` and `channels.feishu.app_secret`.
- Do not use `feishu_send_file` to insert an image into a DocX document. `feishu_send_file` sends a chat attachment only.
- If `create` fails with Wiki `not found` or `permission denied`, verify the configured Wiki node token, app scopes, and knowledge-base membership (`feishu_app_scopes` / `feishu_perm` can help).
- `update_block` only updates text content; non-text blocks may fail.
- `delete_block` expects a child `block_id`, not the document token (`doc_token`).
- `write/append/write_safe` keeps markdown tables by default; if DocX validation still fails, reduce `chunk_chars` and retry with `write_safe`.
- If full-document writes often fail due validation/rate-limit/revision issues, lower `chunk_chars` and prefer `write_safe`.
