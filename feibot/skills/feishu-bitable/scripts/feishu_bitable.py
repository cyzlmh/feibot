#!/usr/bin/env python3
"""Feishu Bitable operations - CLI skill"""
import argparse
import json
import re
import sys
from urllib.parse import parse_qs, urlparse

try:
    import lark_oapi as lark
    import lark_oapi.api.bitable.v1 as lark_bitable
    FEISHU_SDK_AVAILABLE = True
except ImportError:
    FEISHU_SDK_AVAILABLE = False


def parse_bitable_url(url: str) -> tuple[str, str]:
    """Extract app_token and table_id from Bitable URL."""
    parsed = urlparse(url)
    path = parsed.path
    # Pattern: /base/<app_token>/table/<table_id>
    match = re.search(r'/base/([a-zA-Z0-9]+)(?:/table/([a-zA-Z0-9]+))?', path)
    if match:
        return match.group(1), match.group(2) or ""
    return "", ""


def create_client(app_id: str, app_secret: str) -> lark.Client:
    return lark.Client.builder().app_id(app_id).app_secret(app_secret).log_level(lark.LogLevel.WARNING).build()


def action_get_meta(client: lark.Client, app_token: str = "", url: str = "", **kwargs) -> dict:
    """Get Bitable metadata."""
    if not app_token and url:
        app_token, _ = parse_bitable_url(url)
    if not app_token:
        return {"error": "Requires --app-token or --url"}
    
    request = lark_bitable.ListAppTableRequest.builder().app_token(app_token).build()
    response = client.bitable.v1.app_table.list(request)
    if not response.success():
        return {"error": response.msg}
    tables = []
    if response.data and response.data.items:
        for t in response.data.items:
            tables.append({"table_id": t.table_id, "name": t.name})
    return {"app_token": app_token, "tables": tables}


def action_list_fields(client: lark.Client, app_token: str = "", table_id: str = "", url: str = "", **kwargs) -> dict:
    """List table fields."""
    if not app_token and url:
        app_token, _ = parse_bitable_url(url)
    if not app_token:
        return {"error": "Requires --app-token or --url"}
    if not table_id:
        return {"error": "Requires --table-id"}
    
    request = lark_bitable.ListAppTableFieldRequest.builder() \
        .app_token(app_token) \
        .table_id(table_id) \
        .build()
    response = client.bitable.v1.app_table_field.list(request)
    if not response.success():
        return {"error": response.msg}
    fields = []
    if response.data and response.data.items:
        for f in response.data.items:
            field_type = getattr(f, 'type', getattr(f, 'field_type', 'unknown'))
            fields.append({"field_id": f.field_id, "field_name": f.field_name, "field_type": field_type})
    return {"fields": fields}


def action_list_records(client: lark.Client, app_token: str = "", table_id: str = "", url: str = "",
                       page_size: int = 500, **kwargs) -> dict:
    """List table records."""
    if not app_token and url:
        app_token, table_id = parse_bitable_url(url)
    if not app_token or not table_id:
        return {"error": "Requires --app-token and --table-id, or --url"}
    
    request = lark_bitable.ListAppTableRecordRequest.builder() \
        .app_token(app_token) \
        .table_id(table_id) \
        .page_size(page_size) \
        .build()
    response = client.bitable.v1.app_table_record.list(request)
    if not response.success():
        return {"error": response.msg}
    records = []
    if response.data and response.data.items:
        for r in response.data.items:
            records.append({"record_id": r.record_id, "fields": r.fields})
    return {"records": records}


def action_get_record(client: lark.Client, app_token: str = "", table_id: str = "", record_id: str = "", **kwargs) -> dict:
    """Get single record."""
    if not app_token or not table_id or not record_id:
        return {"error": "Requires --app-token, --table-id, and --record-id"}
    
    request = lark_bitable.GetAppTableRecordRequest.builder() \
        .app_token(app_token) \
        .table_id(table_id) \
        .record_id(record_id) \
        .build()
    response = client.bitable.v1.app_table_record.get(request)
    if not response.success():
        return {"error": response.msg}
    r = response.data.record if response.data else None
    return {"record": {"record_id": r.record_id if r else None, "fields": r.fields if r else None}}


def action_create_record(client: lark.Client, app_token: str = "", table_id: str = "", fields: str = "", **kwargs) -> dict:
    """Create record."""
    if not app_token or not table_id:
        return {"error": "Requires --app-token and --table-id"}
    if not fields:
        return {"error": "Requires --fields (JSON string)"}
    
    try:
        fields_dict = json.loads(fields)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON in fields: {e}"}
    
    request = lark_bitable.CreateAppTableRecordRequest.builder() \
        .app_token(app_token) \
        .table_id(table_id) \
        .request_body({"fields": fields_dict}) \
        .build()
    response = client.bitable.v1.app_table_record.create(request)
    if not response.success():
        return {"error": response.msg}
    return {"record_id": response.data.record.record_id if response.data and response.data.record else ""}


def action_update_record(client: lark.Client, app_token: str = "", table_id: str = "", record_id: str = "",
                        fields: str = "", **kwargs) -> dict:
    """Update record."""
    if not app_token or not table_id or not record_id:
        return {"error": "Requires --app-token, --table-id, and --record-id"}
    if not fields:
        return {"error": "Requires --fields (JSON string)"}
    
    try:
        fields_dict = json.loads(fields)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON in fields: {e}"}
    
    request = lark_bitable.UpdateAppTableRecordRequest.builder() \
        .app_token(app_token) \
        .table_id(table_id) \
        .record_id(record_id) \
        .request_body({"fields": fields_dict}) \
        .build()
    response = client.bitable.v1.app_table_record.update(request)
    if not response.success():
        return {"error": response.msg}
    return {"success": True}


def main():
    if not FEISHU_SDK_AVAILABLE:
        print(json.dumps({"error": "lark-oapi not installed"}))
        sys.exit(1)
    
    parser = argparse.ArgumentParser(description="Feishu Bitable operations")
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--app-secret", required=True)
    parser.add_argument("--action", required=True,
                       choices=["get_meta", "list_fields", "list_records", "get_record",
                               "create_record", "update_record", "create_app", "create_field"])
    parser.add_argument("--app-token")
    parser.add_argument("--table-id")
    parser.add_argument("--record-id")
    parser.add_argument("--url")
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--fields", help="JSON string for record fields")
    parser.add_argument("--name", help="For create_app/create_field")
    
    args = vars(parser.parse_args())
    client = create_client(args.pop("app_id"), args.pop("app_secret"))
    action = args.pop("action")
    
    action_map = {
        "get_meta": action_get_meta,
        "list_fields": action_list_fields,
        "list_records": action_list_records,
        "get_record": action_get_record,
        "create_record": action_create_record,
        "update_record": action_update_record,
    }
    handler = action_map.get(action)
    if not handler:
        print(json.dumps({"error": f"Action '{action}' not implemented"}))
        sys.exit(1)
    
    result = handler(client, **args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
