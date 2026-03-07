---
name: feishu-drive
description: "Operate Feishu cloud storage via the `feishu_drive` tool (list/info/create_folder/move/delete). Use when the user needs Drive folder browsing or file placement."
metadata: {"feibot":{"emoji":"🗂️"}}
---

# Feishu Drive Skill

Use `feishu_drive` for Feishu cloud storage (Drive) file and folder operations.

## Workflow

1. Use `action=list` to browse root or a folder before mutating files.
2. Use `action=info` to locate a file in the current folder listing by `file_token`.
3. Use `action=create_folder` to create folders in root or a parent folder.
4. Use `action=move` to place docs/files into the desired folder.
5. Use `action=delete` only when the user explicitly asks to remove a file.

## Tool Call Examples

List root folder:

```json
{
  "action": "list"
}
```

Create a folder:

```json
{
  "action": "create_folder",
  "name": "Project Archive"
}
```

Move a DocX file:

```json
{
  "action": "move",
  "file_token": "doccnABC123",
  "type": "docx",
  "folder_token": "fldcnTARGET123"
}
```

Delete a file:

```json
{
  "action": "delete",
  "file_token": "boxcnFILE123",
  "type": "file"
}
```

## Troubleshooting

- If a `file_token` is "not found" on `info`, list the correct folder first and pass that `folder_token`.
- `move`/`delete` require the correct `type` (for example `docx` vs `file`).
- Permission errors usually mean missing Drive scopes or missing access to the parent folder.
