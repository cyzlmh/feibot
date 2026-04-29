"""OpenAI Codex Responses Provider."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, AsyncGenerator

import httpx
from loguru import logger

from feibot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

DEFAULT_CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_ORIGINATOR = "feibot"


class OpenAICodexProvider(LLMProvider):
    """Use Codex OAuth to call the Responses API."""

    def __init__(self, default_model: str = "openai-codex/gpt-5.1-codex"):
        super().__init__(api_key=None, api_base=None)
        self.default_model = default_model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        del max_tokens, temperature, reasoning_effort, tool_choice  # Unused by Codex Responses API currently.

        model = model or self.default_model
        system_prompt, input_items = _convert_messages(messages)

        try:
            token = await asyncio.to_thread(_get_codex_token)
        except Exception as e:
            return LLMResponse(
                content=(
                    "OpenAI Codex OAuth token not available. "
                    "Authenticate with oauth_cli_kit first. "
                    f"Details: {e}"
                ),
                finish_reason="error",
            )

        headers = _build_headers(token.account_id, token.access)

        body: dict[str, Any] = {
            "model": _strip_model_prefix(model),
            "store": False,
            "stream": True,
            "instructions": system_prompt,
            "input": input_items,
            "text": {"verbosity": "medium"},
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": _prompt_cache_key(messages),
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }

        if tools:
            body["tools"] = _convert_tools(tools)

        try:
            try:
                content, tool_calls, finish_reason = await _request_codex(
                    DEFAULT_CODEX_URL,
                    headers,
                    body,
                    verify=True,
                )
            except Exception as e:
                if "CERTIFICATE_VERIFY_FAILED" not in str(e):
                    raise
                logger.warning("SSL certificate verification failed for Codex API; retrying with verify=False")
                content, tool_calls, finish_reason = await _request_codex(
                    DEFAULT_CODEX_URL,
                    headers,
                    body,
                    verify=False,
                )
            return LLMResponse(
                content=content,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
            )
        except Exception as e:
            return LLMResponse(
                content=f"Error calling Codex: {str(e)}",
                finish_reason="error",
            )

    def get_default_model(self) -> str:
        return self.default_model


def _get_codex_token():
    try:
        from oauth_cli_kit import get_token as get_codex_token
    except ImportError as e:
        raise RuntimeError("oauth_cli_kit is not installed") from e

    token = get_codex_token()
    if not (token and token.access):
        raise RuntimeError("missing OAuth token")
    return token


def _strip_model_prefix(model: str) -> str:
    if model.startswith("openai-codex/") or model.startswith("openai_codex/"):
        return model.split("/", 1)[1]
    return model


def _build_headers(account_id: str, token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": DEFAULT_ORIGINATOR,
        "User-Agent": "feibot (python)",
        "accept": "text/event-stream",
        "content-type": "application/json",
    }


async def _request_codex(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    verify: bool,
) -> tuple[str, list[ToolCallRequest], str]:
    async with httpx.AsyncClient(timeout=60.0, verify=verify) as client:
        async with client.stream("POST", url, headers=headers, json=body) as response:
            if response.status_code != 200:
                text = await response.aread()
                raise RuntimeError(_friendly_error(response.status_code, text.decode("utf-8", "ignore")))
            return await _consume_sse(response)


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI function-calling schema to Codex flat format."""
    converted: list[dict[str, Any]] = []
    for tool in tools:
        fn = (tool.get("function") or {}) if tool.get("type") == "function" else tool
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters") or {}
        converted.append(
            {
                "type": "function",
                "name": name,
                "description": fn.get("description") or "",
                "parameters": params if isinstance(params, dict) else {},
            }
        )
    return converted


def _convert_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system_prompt = ""
    input_items: list[dict[str, Any]] = []

    for idx, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            system_prompt = content if isinstance(content, str) else ""
            continue

        if role == "user":
            input_items.append(_convert_user_message(content))
            continue

        if role == "assistant":
            if isinstance(content, str) and content:
                input_items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                        "status": "completed",
                        "id": f"msg_{idx}",
                    }
                )
            for tool_call in msg.get("tool_calls", []) or []:
                fn = tool_call.get("function") or {}
                call_id, item_id = _split_tool_call_id(tool_call.get("id"))
                call_id = call_id or f"call_{idx}"
                item_id = item_id or f"fc_{idx}"
                input_items.append(
                    {
                        "type": "function_call",
                        "id": item_id,
                        "call_id": call_id,
                        "name": fn.get("name"),
                        "arguments": fn.get("arguments") or "{}",
                    }
                )
            continue

        if role == "tool":
            call_id, _ = _split_tool_call_id(msg.get("tool_call_id"))
            output_text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output_text,
                }
            )

    return system_prompt, input_items


def _convert_user_message(content: Any) -> dict[str, Any]:
    if isinstance(content, str):
        return {"role": "user", "content": [{"type": "input_text", "text": content}]}
    if isinstance(content, list):
        converted: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                converted.append({"type": "input_text", "text": item.get("text", "")})
            elif item.get("type") == "image_url":
                url = (item.get("image_url") or {}).get("url")
                if url:
                    converted.append({"type": "input_image", "image_url": url, "detail": "auto"})
        if converted:
            return {"role": "user", "content": converted}
    return {"role": "user", "content": [{"type": "input_text", "text": ""}]}


def _split_tool_call_id(tool_call_id: Any) -> tuple[str, str | None]:
    if isinstance(tool_call_id, str) and tool_call_id:
        if "|" in tool_call_id:
            call_id, item_id = tool_call_id.split("|", 1)
            return call_id, item_id or None
        return tool_call_id, None
    return "call_0", None


def _prompt_cache_key(messages: list[dict[str, Any]]) -> str:
    raw = json.dumps(messages, ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _iter_sse(response: httpx.Response) -> AsyncGenerator[dict[str, Any], None]:
    buffer: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if buffer:
                data_lines = [chunk[5:].strip() for chunk in buffer if chunk.startswith("data:")]
                buffer = []
                if not data_lines:
                    continue
                data = "\n".join(data_lines).strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    yield json.loads(data)
                except Exception:
                    continue
            continue
        buffer.append(line)


async def _consume_sse(response: httpx.Response) -> tuple[str, list[ToolCallRequest], str]:
    content = ""
    tool_calls: list[ToolCallRequest] = []
    tool_call_buffers: dict[str, dict[str, Any]] = {}
    finish_reason = "stop"

    async for event in _iter_sse(response):
        event_type = event.get("type")
        if event_type == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                if call_id:
                    tool_call_buffers.setdefault(
                        call_id,
                        {
                            "item_id": item.get("id") or "",
                            "name": item.get("name") or "",
                            "arguments": "",
                        },
                    )
            continue

        if event_type == "response.output_text.delta":
            content += event.get("delta") or ""
            continue

        if event_type == "response.function_call_arguments.delta":
            call_id = event.get("call_id")
            if call_id:
                buf = tool_call_buffers.setdefault(call_id, {"item_id": "", "name": "", "arguments": ""})
                buf["arguments"] += event.get("delta") or ""
            continue

        if event_type == "response.function_call_arguments.done":
            call_id = event.get("call_id")
            if call_id:
                buf = tool_call_buffers.setdefault(call_id, {"item_id": "", "name": "", "arguments": ""})
                if event.get("arguments"):
                    buf["arguments"] = event.get("arguments") or ""
            continue

        if event_type == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                if call_id:
                    buf = tool_call_buffers.setdefault(call_id, {"item_id": "", "name": "", "arguments": ""})
                    buf["item_id"] = item.get("id") or buf.get("item_id") or ""
                    buf["name"] = item.get("name") or buf.get("name") or ""
                    if item.get("arguments"):
                        buf["arguments"] = item.get("arguments")
            continue

        if event_type == "response.completed":
            response_obj = event.get("response") or {}
            status = response_obj.get("status")
            if status != "completed":
                details = response_obj.get("error") or {}
                code = details.get("code") or "unknown"
                message = details.get("message") or "unknown error"
                raise RuntimeError(f"Codex response failed: {code} - {message}")

            out = response_obj.get("output") or []
            for item in out:
                if item.get("type") == "message":
                    for block in item.get("content") or []:
                        if block.get("type") == "output_text":
                            content += block.get("text") or ""
                elif item.get("type") == "function_call":
                    call_id = item.get("call_id")
                    if not call_id:
                        continue
                    buf = tool_call_buffers.setdefault(call_id, {"item_id": "", "name": "", "arguments": ""})
                    buf["item_id"] = item.get("id") or buf.get("item_id") or ""
                    buf["name"] = item.get("name") or buf.get("name") or ""
                    if item.get("arguments"):
                        buf["arguments"] = item.get("arguments")

            for call_id, item in tool_call_buffers.items():
                raw_args = item.get("arguments") or "{}"
                try:
                    parsed_args = json.loads(raw_args)
                except Exception:
                    parsed_args = {"raw": raw_args}
                item_id = item.get("item_id") or ""
                full_call_id = f"{call_id}|{item_id}" if item_id else call_id
                tool_calls.append(
                    ToolCallRequest(
                        id=full_call_id,
                        name=item.get("name") or "tool",
                        arguments=parsed_args,
                    )
                )
            finish_reason = "tool_calls" if tool_calls else "stop"
            return content, tool_calls, finish_reason

        if event_type == "response.failed":
            raise RuntimeError("Codex response failed")

    return content, tool_calls, finish_reason


def _friendly_error(status: int, body: str) -> str:
    if status == 401:
        return "unauthorized (401). Refresh your oauth_cli_kit token and try again."
    if status == 403:
        return "forbidden (403). Your account may not have Codex access."
    if status == 429:
        return "rate limited (429). Please retry in a moment."
    snippet = body.strip().replace("\n", " ")
    if len(snippet) > 200:
        snippet = snippet[:200] + "..."
    return f"HTTP {status}: {snippet}"
