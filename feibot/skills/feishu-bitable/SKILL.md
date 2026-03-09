---
name: feishu-bitable
description: Operate Feishu Bitable via CLI. Actions include get_meta, list_fields, list_records, get_record, create_record, update_record. Use when the user wants to read/write Feishu multi-dimensional tables.
---

# Feishu Bitable Skill

Operate Feishu multi-dimensional tables.

## Usage

```bash
python scripts/feishu_bitable.py --app-id <ID> --app-secret <SECRET> --action <ACTION>
```

## Actions

| Action | Required | Description |
|--------|----------|-------------|
| `get_meta` | `--app-token` or `--url` | List tables in app |
| `list_fields` | `--app-token`, `--table-id` | List table fields |
| `list_records` | `--app-token`, `--table-id` | List records |
| `get_record` | `--app-token`, `--table-id`, `--record-id` | Get single record |
| `create_record` | `--app-token`, `--table-id`, `--fields` | Create record |
| `update_record` | `--app-token`, `--table-id`, `--record-id`, `--fields` | Update record |

## Examples

```bash
# Get tables from URL
python scripts/feishu_bitable.py --app-id $APP_ID --app-secret $APP_SECRET \
  --action get_meta --url "https://xxx.feishu.cn/base/xxx"

# List records
python scripts/feishu_bitable.py --app-id $APP_ID --app-secret $APP_SECRET \
  --action list_records --app-token "xxx" --table-id "xxx"

# Create record
python scripts/feishu_bitable.py --app-id $APP_ID --app-secret $APP_SECRET \
  --action create_record --app-token "xxx" --table-id "xxx" \
  --fields '{"Name": "Test", "Status": "Done"}'
```
