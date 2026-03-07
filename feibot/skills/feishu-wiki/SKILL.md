---
name: feishu-wiki
description: "Navigate Feishu knowledge bases via the `feishu_wiki` tool (spaces/nodes/get/create/move/rename). Use when the user mentions Wiki, knowledge base, or /wiki/ links."
metadata: {"feibot":{"emoji":"📚"}}
---

# Feishu Wiki Skill

Use `feishu_wiki` for Feishu knowledge-base (Wiki) operations.

## Workflow

1. If the user gives a Wiki URL, extract the `/wiki/<token>` value or pass the URL to `feishu_wiki` where supported.
2. Use `action=get` to inspect a node and find `obj_type` / `obj_token`.
3. Use `obj_token` with `feishu_doc` (for `docx`) or Bitable tools (for `bitable`) for content editing.
4. Use `action=spaces` and `action=nodes` to discover valid `space_id` / `parent_node_token` instead of guessing tokens.
5. Use `action=create` to create nodes inside a known Wiki space/parent.

## Tool Call Examples

List spaces:

```json
{
  "action": "spaces"
}
```

Get a Wiki node:

```json
{
  "action": "get",
  "url": "https://example.feishu.cn/wiki/JnYiwVERWiPzxQk6NbzcK6E3nje"
}
```

Create a DocX page under a parent node:

```json
{
  "action": "create",
  "space_id": "7358544912177356801",
  "parent_node_token": "wikcnParent123",
  "title": "Project Notes",
  "obj_type": "docx"
}
```

## Troubleshooting

- `code=131005 not found`: wrong `space_id` or `parent_node_token` (often a guessed token).
- `code=131006 permission denied`: bot/app is not a Wiki member or lacks edit/admin access.
- If the bot cannot be selected in the UI, grant access via a group and use `feishu_perm` to verify membership.
