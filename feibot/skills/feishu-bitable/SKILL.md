---
name: feishu-bitable
description: "Operate Feishu Bitable via `feishu_bitable_*` tools (parse URL, inspect fields, list/get/create/update records, create app, create field). Use when the user wants the agent to read/write Feishu multi-dimensional tables."
metadata: {"feibot":{"emoji":"🧮"}}
---

# Feishu Bitable Skill

Use the `feishu_bitable_*` tools for Feishu Bitable operations.

## Recommended Order

1. Start with `feishu_bitable_get_meta` when given a Feishu Bitable/base URL.
2. Call `feishu_bitable_list_fields` before writing records to confirm field names and types.
3. If a required column is missing, use `feishu_bitable_create_field` before writing records.
4. Use `feishu_bitable_create_record` or `feishu_bitable_update_record`.
5. Use `feishu_bitable_list_records` / `feishu_bitable_get_record` to verify results.

## Wiki URL Caveat

If the user provides a Wiki URL (`/wiki/...`), `feishu_bitable_get_meta` may not be able to extract `app_token` directly. Ask for:

- a `/base/...` URL, or
- explicit `app_token` and `table_id`

## Tool Call Examples

Parse a URL:

```json
{
  "url": "https://example.feishu.cn/base/bascnABC123?table=tblXYZ789"
}
```

List fields:

```json
{
  "app_token": "bascnABC123",
  "table_id": "tblXYZ789"
}
```

Create a record:

```json
{
  "app_token": "bascnABC123",
  "table_id": "tblXYZ789",
  "fields": {
    "Title": "LLM created row",
    "Status": "Todo"
  }
}
```

Update a record:

```json
{
  "app_token": "bascnABC123",
  "table_id": "tblXYZ789",
  "record_id": "recn123",
  "fields": {
    "Status": "Done"
  }
}
```

## Troubleshooting

- If API calls fail with permissions, check Feishu app scopes for Bitable/Drive.
- "Row number" in the UI is not a stable API identifier; use `record_id`.
