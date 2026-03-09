---
name: feishu-drive
description: Operate Feishu cloud storage via CLI. Actions include list, info, create_folder, move, delete. Use when the user needs Drive folder browsing or file management.
---

# Feishu Drive Skill

Operate Feishu cloud storage.

## Usage

```bash
python scripts/feishu_drive.py --app-id <ID> --app-secret <SECRET> --action <ACTION>
```

## Actions

| Action | Required | Description |
|--------|----------|-------------|
| `list` | `--folder-token` (optional) | List folder contents |
| `info` | `--file-token` | Get file info |
| `create_folder` | `--name` | Create new folder |

## Examples

```bash
python scripts/feishu_drive.py --app-id $APP_ID --app-secret $APP_SECRET --action list
python scripts/feishu_drive.py --app-id $APP_ID --app-secret $APP_SECRET --action create_folder --name "New Folder"
```
