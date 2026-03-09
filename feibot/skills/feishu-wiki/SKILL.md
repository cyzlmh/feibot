---
name: feishu-wiki
description: Navigate Feishu knowledge bases via CLI. Actions include spaces, nodes, get, create, move, rename. Use when the user mentions Wiki, knowledge base, or /wiki/ links.
---

# Feishu Wiki Skill

Navigate Feishu knowledge bases.

## Usage

```bash
python scripts/feishu_wiki.py --app-id <ID> --app-secret <SECRET> --action <ACTION> [options]
```

## Actions

| Action | Required | Description |
|--------|----------|-------------|
| `spaces` | - | List all wiki spaces |
| `nodes` | `--space-id` | List nodes in space |
| `get` | `--token` or `--url` | Get node detail |

## Options

- `--app-id`, `--app-secret`: Credentials (required)
- `--space-id`: Space ID
- `--parent-node-token`: Parent node for listing
- `--token`: Node token
- `--url`: Wiki URL

## Examples

```bash
python scripts/feishu_wiki.py --app-id $APP_ID --app-secret $APP_SECRET --action spaces
python scripts/feishu_wiki.py --app-id $APP_ID --app-secret $APP_SECRET --action nodes --space-id 123
```
