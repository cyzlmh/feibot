---
name: feishu-perm
description: "Manage Feishu document/file/wiki collaborators via the `feishu_perm` tool (list/add/remove). Use when the user asks about sharing, permissions, or collaborator access."
metadata: {"feibot":{"emoji":"🔐"}}
---

# Feishu Permission Skill

Use `feishu_perm` to list or modify permission members for Drive/Wiki-backed resources.

## Workflow

1. Confirm the target `token` and `type` (`docx`, `wiki`, `folder`, `file`, etc.).
2. Start with `action=list` to inspect current collaborators.
3. Use `action=add` or `action=remove` for changes.
4. Prefer `member_type=groupid` when the bot/app cannot be selected directly in the Feishu UI.
5. For permission failures, check app scopes first with `feishu_app_scopes`.

## Tool Call Examples

List collaborators:

```json
{
  "action": "list",
  "token": "doccnABC123",
  "type": "docx"
}
```

Add a group as editor:

```json
{
  "action": "add",
  "token": "wikcnABC123",
  "type": "wiki",
  "member_type": "groupid",
  "member_id": "oc_xxx_group_id",
  "perm": "edit"
}
```

Remove a collaborator:

```json
{
  "action": "remove",
  "token": "doccnABC123",
  "type": "docx",
  "member_type": "openid",
  "member_id": "ou_xxx"
}
```

## Troubleshooting

- If `add/remove` fails, verify the app has Drive permission-member scopes and the app was re-authorized after scope changes.
- `member_type` and `member_id` must match (for example `groupid` with a group id, `openid` with an open_id).
- `full_access` is required for members who need to manage permissions.
