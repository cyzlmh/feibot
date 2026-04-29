"""Tests for OpenAICompatProvider spec-driven behavior."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from feibot.providers.openai_compat_provider import OpenAICompatProvider
from feibot.providers.registry import find_by_name


def _fake_chat_response(content: str = "ok") -> SimpleNamespace:
    message = SimpleNamespace(
        content=content,
        tool_calls=None,
        reasoning_content=None,
    )
    choice = SimpleNamespace(message=message, finish_reason="stop")
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    return SimpleNamespace(choices=[choice], usage=usage)


def test_openrouter_spec_is_gateway() -> None:
    spec = find_by_name("openrouter")
    assert spec is not None
    assert spec.is_gateway is True
    assert spec.default_api_base == "https://openrouter.ai/api/v1"


def test_openrouter_keeps_model_name_intact() -> None:
    mock_create = AsyncMock(return_value=_fake_chat_response())
    spec = find_by_name("openrouter")

    with patch("feibot.providers.openai_compat_provider.AsyncOpenAI") as MockClient:
        client_instance = MockClient.return_value
        client_instance.chat.completions.create = mock_create

        provider = OpenAICompatProvider(
            api_key="sk-or-test-key",
            api_base="https://openrouter.ai/api/v1",
            default_model="anthropic/claude-sonnet-4-5",
            spec=spec,
        )
        asyncio.run(
            provider.chat(
                messages=[{"role": "user", "content": "hello"}],
                model="anthropic/claude-sonnet-4-5",
            )
        )

    assert mock_create.call_args.kwargs["model"] == "anthropic/claude-sonnet-4-5"


def test_aihubmix_strips_model_prefix() -> None:
    mock_create = AsyncMock(return_value=_fake_chat_response())
    spec = find_by_name("aihubmix")

    with patch("feibot.providers.openai_compat_provider.AsyncOpenAI") as MockClient:
        client_instance = MockClient.return_value
        client_instance.chat.completions.create = mock_create

        provider = OpenAICompatProvider(
            api_key="sk-aihub-test-key",
            api_base="https://aihubmix.com/v1",
            default_model="claude-sonnet-4-5",
            spec=spec,
        )
        asyncio.run(
            provider.chat(
                messages=[{"role": "user", "content": "hello"}],
                model="anthropic/claude-sonnet-4-5",
            )
        )

    assert mock_create.call_args.kwargs["model"] == "claude-sonnet-4-5"


def test_reasoning_effort_is_forwarded() -> None:
    mock_create = AsyncMock(return_value=_fake_chat_response())
    spec = find_by_name("openai")

    with patch("feibot.providers.openai_compat_provider.AsyncOpenAI") as MockClient:
        client_instance = MockClient.return_value
        client_instance.chat.completions.create = mock_create

        provider = OpenAICompatProvider(
            api_key="sk-openai-test-key",
            default_model="gpt-4o-mini",
            spec=spec,
        )
        asyncio.run(
            provider.chat(
                messages=[{"role": "user", "content": "hello"}],
                reasoning_effort="high",
            )
        )

    assert mock_create.call_args.kwargs["reasoning_effort"] == "high"


def test_openai_model_passthrough() -> None:
    spec = find_by_name("openai")
    with patch("feibot.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider(
            api_key="sk-test-key",
            default_model="gpt-4o",
            spec=spec,
        )
    assert provider.get_default_model() == "gpt-4o"


def test_openai_prefixed_model_is_stripped_for_request() -> None:
    mock_create = AsyncMock(return_value=_fake_chat_response())
    spec = find_by_name("openai")

    with patch("feibot.providers.openai_compat_provider.AsyncOpenAI") as MockClient:
        client_instance = MockClient.return_value
        client_instance.chat.completions.create = mock_create

        provider = OpenAICompatProvider(
            api_key="sk-openai-test-key",
            default_model="openai/gpt-4o-mini",
            spec=spec,
        )
        result = asyncio.run(
            provider.chat(
                messages=[{"role": "user", "content": "hello"}],
                model="openai/glm-5",
            )
        )

    assert mock_create.call_args.kwargs["model"] == "glm-5"
    assert result.model == "glm-5"


def test_parse_accepts_dict_response() -> None:
    with patch("feibot.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider()

    result = provider._parse(
        {
            "choices": [
                {
                    "message": {"content": "hello from dict"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 2,
                "total_tokens": 3,
            },
        },
        requested_model="openai/gpt-4o-mini",
    )

    assert result.finish_reason == "stop"
    assert result.content == "hello from dict"
    assert result.usage["total_tokens"] == 3
    assert result.provider_payload is not None
    assert result.provider_payload["requested_model"] == "openai/gpt-4o-mini"
