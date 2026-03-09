#!/usr/bin/env python3
"""Feishu Wiki operations - CLI skill"""
import argparse
import json
import re
import sys
from urllib.parse import urlparse

try:
    import lark_oapi as lark
    import lark_oapi.api.wiki.v2 as lark_wiki_v2
    FEISHU_SDK_AVAILABLE = True
except ImportError:
    FEISHU_SDK_AVAILABLE = False


def create_client(app_id: str, app_secret: str) -> lark.Client:
    return lark.Client.builder().app_id(app_id).app_secret(app_secret).log_level(lark.LogLevel.WARNING).build()


def action_spaces(client: lark.Client, **kwargs) -> dict:
    """List wiki spaces."""
    request = lark_wiki_v2.ListSpaceRequest.builder().build()
    response = client.wiki.v2.space.list(request)
    if not response.success():
        return {"error": response.msg}
    spaces = []
    if response.data and response.data.items:
        for space in response.data.items:
            spaces.append({"space_id": space.space_id, "name": space.name})
    return {"spaces": spaces}


def action_nodes(client: lark.Client, space_id: str, parent_node_token: str = "", **kwargs) -> dict:
    """List wiki nodes."""
    request = lark_wiki_v2.ListSpaceNodeRequest.builder() \
        .space_id(space_id) \
        .parent_node_token(parent_node_token) \
        .build()
    response = client.wiki.v2.space_node.list(request)
    if not response.success():
        return {"error": response.msg}
    nodes = []
    if response.data and response.data.items:
        for node in response.data.items:
            nodes.append({
                "node_token": node.node_token,
                "title": node.title,
                "obj_type": node.obj_type
            })
    return {"nodes": nodes}


def action_get(client: lark.Client, token: str = "", url: str = "", **kwargs) -> dict:
    """Get wiki node detail."""
    node_token = token
    if not node_token and url:
        parsed = urlparse(url)
        match = re.search(r'/wiki/([a-zA-Z0-9]+)', parsed.path)
        if match:
            node_token = match.group(1)
    if not node_token:
        return {"error": "Requires --token or --url"}
    
    request = lark_wiki_v2.GetSpaceNodeRequest.builder().node_token(node_token).build()
    response = client.wiki.v2.space_node.get(request)
    if not response.success():
        return {"error": response.msg}
    node = response.data.node if response.data else None
    return {
        "node": {
            "node_token": node.node_token if node else None,
            "title": node.title if node else None,
            "obj_type": node.obj_type if node else None
        }
    }


def main():
    if not FEISHU_SDK_AVAILABLE:
        print(json.dumps({"error": "lark-oapi not installed"}))
        sys.exit(1)
    
    parser = argparse.ArgumentParser(description="Feishu Wiki operations")
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--app-secret", required=True)
    parser.add_argument("--action", required=True, choices=["spaces", "nodes", "get", "create", "move", "rename"])
    parser.add_argument("--space-id")
    parser.add_argument("--parent-node-token", default="")
    parser.add_argument("--token")
    parser.add_argument("--url")
    parser.add_argument("--title")
    
    args = vars(parser.parse_args())
    client = create_client(args.pop("app_id"), args.pop("app_secret"))
    action = args.pop("action")
    
    action_map = {"spaces": action_spaces, "nodes": action_nodes, "get": action_get}
    handler = action_map.get(action)
    if not handler:
        print(json.dumps({"error": f"Action '{action}' not implemented"}))
        sys.exit(1)
    
    result = handler(client, **args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
