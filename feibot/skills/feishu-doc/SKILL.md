---
name: feishu-doc
description: Operate Feishu DocX documents including read, create, list blocks, get block, and update operations. Use when the user needs to read document content, create new documents, list document blocks, or manipulate DocX files in Feishu/Lark. Requires Feishu App ID and App Secret.
---

# Feishu Doc Skill

Operate Feishu DocX documents via CLI script.

## Usage

```bash
python scripts/feishu_doc.py --app-id <APP_ID> --app-secret <APP_SECRET> --action <ACTION> [options]
```

## Actions

| Action | Required Params | Description |
|--------|-----------------|-------------|
| `read` | `--doc-token` or `--url` | Read document content |
| `list_blocks` | `--doc-token` or `--url` | List all blocks in document |
| `get_block` | `--doc-token`, `--block-id` | Get specific block |
| `create` | `--title` | Create new document |

## Options

- `--app-id`: Feishu App ID (required)
- `--app-secret`: Feishu App Secret (required)
- `--action`: Action to perform (required)
- `--doc-token`: Document token
- `--url`: DocX URL (alternative to doc-token)
- `--title`: Document title (for create action)
- `--content`: Content (for write/append actions)
- `--block-id`: Block ID (for get/update/delete)
- `--page-size`: Page size for list_blocks (default: 200)

## Examples

```bash
# Read document
python scripts/feishu_doc.py --app-id $APP_ID --app-secret $APP_SECRET \
  --action read --doc-token "doxxx"

# Create document
python scripts/feishu_doc.py --app-id $APP_ID --app-secret $APP_SECRET \
  --action create --title "My Document"

# List blocks
python scripts/feishu_doc.py --app-id $APP_ID --app-secret $APP_SECRET \
  --action list_blocks --url "https://xxx.feishu.cn/docx/xxx"
```

## Output

All actions output JSON to stdout. Errors include an `error` field.
