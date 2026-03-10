from types import SimpleNamespace

import pytest

from feibot.providers.litellm_provider import LiteLLMProvider


@pytest.mark.asyncio
async def test_litellm_provider_passes_reasoning_effort_from_env(monkeypatch) -> None:
    monkeypatch.setenv("FEIBOT_REASONING_EFFORT", "high")
    provider = LiteLLMProvider(api_key="test-key", default_model="openai/gpt-4o-mini")
    captured: dict = {}

    async def _fake_call_with_retries(*, kwargs, max_retries, base_delay, max_delay):  # noqa: ANN001
        captured.update(kwargs)
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok", tool_calls=None, reasoning_content=None),
                    finish_reason="stop",
                )
            ],
            model="openai/gpt-4o-mini-2026-01-01",
            usage=None,
        )
        return response, None

    monkeypatch.setattr(provider, "_call_with_retries", _fake_call_with_retries)

    result = await provider.chat(messages=[{"role": "user", "content": "hello"}])

    assert result.content == "ok"
    assert result.model == "openai/gpt-4o-mini-2026-01-01"
    assert result.provider_payload is not None
    assert result.provider_payload["requested_model"] == "openai/gpt-4o-mini"
    assert result.provider_payload["response_model"] == "openai/gpt-4o-mini-2026-01-01"
    assert result.provider_payload["message"]["content"] == "ok"
    assert captured.get("reasoning_effort") == "high"
