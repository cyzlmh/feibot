#!/usr/bin/env python3
"""
Feishu DocX operations - CLI skill for feibot
Compatible with all LLM models including CodeX
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    import lark_oapi as lark
    import lark_oapi.api.docx.v1 as lark_docx
    import lark_oapi.api.wiki.v2 as lark_wiki_v2
    FEISHU_SDK_AVAILABLE = True
except ImportError:
    FEISHU_SDK_AVAILABLE = False


def resolve_doc_token_from_url(url: str) -> tuple[str, bool]:
    """Resolve doc_token from DocX URL and return (token, is_wiki)."""
    parsed = urlparse(url)
    path = parsed.path
    is_wiki = "/wiki/" in path
    
    patterns = [
        r'/docx/([a-zA-Z0-9]+)',
        r'/wiki/([a-zA-Z0-9]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, path)
        if match:
            return match.group(1), is_wiki
    return "", is_wiki


def create_client(app_id: str, app_secret: str) -> lark.Client:
    """Create Feishu client."""
    return lark.Client.builder() \
        .app_id(app_id) \
        .app_secret(app_secret) \
        .log_level(lark.LogLevel.WARNING) \
        .build()


def action_read(client: lark.Client, doc_token: str, **kwargs) -> dict:
    """Read document content."""
    request = lark_docx.GetDocumentContentRequest.builder() \
        .document_id(doc_token) \
        .build()
    response = client.docx.v1.document_content.get(request)
    
    if not response.success():
        return {"error": f"Failed to read document: {response.msg}"}
    
    return {"content": response.data.content if response.data else ""}


def action_list_blocks(client: lark.Client, doc_token: str, page_size: int = 200, **kwargs) -> dict:
    """List document blocks."""
    request = lark_docx.ListDocumentBlockRequest.builder() \
        .document_id(doc_token) \
        .page_size(page_size) \
        .build()
    response = client.docx.v1.document_block.list(request)
    
    if not response.success():
        return {"error": f"Failed to list blocks: {response.msg}"}
    
    blocks = []
    if response.data and response.data.items:
        for block in response.data.items:
            blocks.append({
                "block_id": block.block_id,
                "block_type": block.block_type,
                "parent_id": block.parent_id
            })
    return {"blocks": blocks}


def action_get_block(client: lark.Client, doc_token: str, block_id: str, **kwargs) -> dict:
    """Get specific block."""
    request = lark_docx.GetDocumentBlockRequest.builder() \
        .document_id(doc_token) \
        .block_id(block_id) \
        .build()
    response = client.docx.v1.document_block.get(request)
    
    if not response.success():
        return {"error": f"Failed to get block: {response.msg}"}
    
    block = response.data.block if response.data else None
    return {
        "block": {
            "block_id": block.block_id if block else None,
            "block_type": block.block_type if block else None
        }
    }


def action_create(client: lark.Client, title: str, content: str = "", 
                  folder_token: str = "", owner_open_id: str = "", **kwargs) -> dict:
    """Create new document."""
    request = lark_docx.CreateDocumentRequest.builder() \
        .request_body(lark_docx.CreateDocumentRequestBody.builder()
            .title(title)
            .folder_token(folder_token)
            .build()) \
        .build()
    response = client.docx.v1.document.create(request)
    
    if not response.success():
        return {"error": f"Failed to create document: {response.msg}"}
    
    doc_token = response.data.document.document_id if response.data and response.data.document else ""
    
    # Add content if provided
    if content and doc_token:
        # Simple text block creation
        pass  # TODO: implement content insertion
    
    return {
        "doc_token": doc_token,
        "url": response.data.document.url if response.data and response.data.document else ""
    }


def main():
    if not FEISHU_SDK_AVAILABLE:
        print(json.dumps({"error": "lark-oapi not installed. Run: pip install lark-oapi"}))
        sys.exit(1)
    
    parser = argparse.ArgumentParser(description="Feishu DocX operations")
    parser.add_argument("--app-id", required=True, help="Feishu App ID")
    parser.add_argument("--app-secret", required=True, help="Feishu App Secret")
    parser.add_argument("--action", required=True, 
                       choices=["read", "list_blocks", "get_block", "create", 
                               "update_block", "delete_block", "append", "write", "write_safe", "insert_image"],
                       help="Action to perform")
    parser.add_argument("--doc-token", help="Document token")
    parser.add_argument("--url", help="DocX URL (alternative to doc-token)")
    parser.add_argument("--title", help="Document title (for create)")
    parser.add_argument("--content", help="Content (for write/append)")
    parser.add_argument("--block-id", help="Block ID")
    parser.add_argument("--page-size", type=int, default=200, help="Page size for list_blocks")
    
    args = parser.parse_args()
    
    # Resolve doc_token from url if needed
    doc_token = args.doc_token
    if not doc_token and args.url:
        doc_token, _ = resolve_doc_token_from_url(args.url)
    
    # Validate required params
    if args.action in ["read", "list_blocks", "get_block", "update_block", "delete_block", "append", "write", "write_safe", "insert_image"]:
        if not doc_token:
            print(json.dumps({"error": f"action '{args.action}' requires --doc-token or --url"}))
            sys.exit(1)
    
    if args.action == "create" and not args.title:
        print(json.dumps({"error": "action 'create' requires --title"}))
        sys.exit(1)
    
    if args.action in ["get_block", "update_block", "delete_block"] and not args.block_id:
        print(json.dumps({"error": f"action '{args.action}' requires --block-id"}))
        sys.exit(1)
    
    # Create client and execute
    client = create_client(args.app_id, args.app_secret)
    
    action_map = {
        "read": action_read,
        "list_blocks": action_list_blocks,
        "get_block": action_get_block,
        "create": action_create,
    }
    
    handler = action_map.get(args.action)
    if not handler:
        print(json.dumps({"error": f"Action '{args.action}' not yet implemented"}))
        sys.exit(1)
    
    result = handler(client, doc_token=doc_token, title=args.title, content=args.content,
                    block_id=args.block_id, page_size=args.page_size)
    
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
