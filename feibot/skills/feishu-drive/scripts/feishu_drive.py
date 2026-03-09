#!/usr/bin/env python3
"""Feishu Drive operations - CLI skill"""
import argparse
import json
import sys

try:
    import lark_oapi as lark
    import lark_oapi.api.drive.v1 as lark_drive
    FEISHU_SDK_AVAILABLE = True
except ImportError:
    FEISHU_SDK_AVAILABLE = False


def create_client(app_id: str, app_secret: str) -> lark.Client:
    return lark.Client.builder().app_id(app_id).app_secret(app_secret).log_level(lark.LogLevel.WARNING).build()


def action_list(client: lark.Client, folder_token: str = "", **kwargs) -> dict:
    """List folder contents."""
    request = lark_drive.ListFileRequest.builder() \
        .folder_token(folder_token) \
        .build()
    response = client.drive.v1.file.list(request)
    if not response.success():
        return {"error": response.msg}
    files = []
    if response.data and response.data.files:
        for f in response.data.files:
            files.append({"name": f.name, "token": f.token, "type": f.type})
    return {"files": files}


def action_info(client: lark.Client, file_token: str = "", **kwargs) -> dict:
    """Get file info."""
    if not file_token:
        return {"error": "Requires --file-token"}
    request = lark_drive.GetFileRequest.builder().file_token(file_token).build()
    response = client.drive.v1.file.get(request)
    if not response.success():
        return {"error": response.msg}
    f = response.data.file if response.data else None
    return {"file": {"name": f.name if f else None, "token": f.token if f else None}}


def action_create_folder(client: lark.Client, name: str = "", folder_token: str = "", **kwargs) -> dict:
    """Create folder."""
    if not name:
        return {"error": "Requires --name"}
    request = lark_drive.CreateFolderRequest.builder() \
        .request_body(lark_drive.CreateFolderRequestBody.builder()
            .name(name)
            .folder_token(folder_token)
            .build()) \
        .build()
    response = client.drive.v1.file.create_folder(request)
    if not response.success():
        return {"error": response.msg}
    return {"folder_token": response.data.token if response.data else ""}


def main():
    if not FEISHU_SDK_AVAILABLE:
        print(json.dumps({"error": "lark-oapi not installed"}))
        sys.exit(1)
    
    parser = argparse.ArgumentParser(description="Feishu Drive operations")
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--app-secret", required=True)
    parser.add_argument("--action", required=True, choices=["list", "info", "create_folder", "move", "delete"])
    parser.add_argument("--folder-token", default="")
    parser.add_argument("--file-token")
    parser.add_argument("--name")
    
    args = vars(parser.parse_args())
    client = create_client(args.pop("app_id"), args.pop("app_secret"))
    action = args.pop("action")
    
    action_map = {"list": action_list, "info": action_info, "create_folder": action_create_folder}
    handler = action_map.get(action)
    if not handler:
        print(json.dumps({"error": f"Action '{action}' not implemented"}))
        sys.exit(1)
    
    result = handler(client, **args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
