---
name: feishu-file-send
description: "Send local files to Feishu/Lark by calling the `feishu_send_file` tool. Use when the user asks to deliver an existing workspace file (for example a blog post markdown or a Python script) to Feishu instead of pasting text."
metadata: {"feibot":{"emoji":"📎"}}
---

# Feishu File Send Skill

Use `feishu_send_file` whenever the user asks to send a local file to Feishu.

## Workflow

1. Confirm the exact file path.
2. If the path is uncertain, use `list_dir` and `read_file` to locate the right file.
3. Call `feishu_send_file` with `file_path`.
4. Add `note` when the user asks for context text before the file.
5. If needed, set `receive_id` and `receive_id_type` explicitly.

## Tool Call Examples

Basic send:

```json
{
  "file_path": "/absolute/path/to/file.md"
}
```

Send with note:

```json
{
  "file_path": "/absolute/path/to/file.py",
  "note": "你要的脚本，见附件。"
}
```

Send to a specific group chat:

```json
{
  "file_path": "/absolute/path/to/file.md",
  "receive_id": "oc_xxx",
  "receive_id_type": "chat_id"
}
```

## Troubleshooting

- If result says credentials are missing, configure `channels.feishu.app_id` and `channels.feishu.app_secret`.
- If result says receive target is missing, pass `receive_id` or set `channels.feishu.allow_from`.
- If result says path not allowed, use a file inside the current workspace when `restrict_to_workspace` is enabled.
- If result says missing scopes (`im:resource:upload` or `im:resource`), open Feishu Open Platform app permissions and grant one of these scopes first.
