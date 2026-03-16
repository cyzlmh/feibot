import os

import litellm

from feibot.providers.litellm_provider import LiteLLMProvider


def test_llm_policy_overrides_env(monkeypatch) -> None:
    monkeypatch.setenv("FEIBOT_LLM_MAX_RETRIES", "9")
    monkeypatch.setenv("FEIBOT_LLM_RETRY_BASE_DELAY_SEC", "9")
    monkeypatch.setenv("FEIBOT_LLM_RETRY_MAX_DELAY_SEC", "9")
    monkeypatch.setenv("FEIBOT_LLM_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("FEIBOT_LLM_FALLBACK_RETRYABLE_ONLY", "true")

    provider = LiteLLMProvider(
        default_model="openai/gpt-4o-mini",
        llm_policy={
            "retry": {
                "max_retries": 1,
                "base_delay_sec": 0.2,
                "max_delay_sec": 0.4,
            },
            "fallback": {
                "enabled": False,
                "retryable_errors_only": False,
            },
        },
    )

    assert provider._retry_settings() == (1, 0.2, 0.4)
    assert provider._fallback_settings() == (False, False)


def test_retryable_status_codes_can_be_configured(monkeypatch) -> None:
    monkeypatch.delenv("FEIBOT_LLM_RETRYABLE_STATUS_CODES", raising=False)

    provider = LiteLLMProvider(
        default_model="openai/gpt-4o-mini",
        llm_policy={"retry": {"retryable_status_codes": [418]}},
    )

    class TeapotError(Exception):
        status_code = 418

    class InternalError(Exception):
        status_code = 500

    assert provider._is_retryable_error(TeapotError("teapot"))
    assert not provider._is_retryable_error(InternalError("internal"))


def test_kimi_coding_model_ref_is_rewritten_to_anthropic_messages() -> None:
    provider = LiteLLMProvider(default_model="kimi-coding/k2p5")

    assert provider._resolve_model("kimi-coding/k2p5") == "anthropic/k2p5"
    assert provider._resolve_model("kimi-coding/kimi-for-coding") == "anthropic/k2p5"


def test_kimi_coding_api_base_uses_kimi_coding_endpoint_when_moonshot_base_configured() -> None:
    provider = LiteLLMProvider(
        default_model="kimi-coding/k2p5",
        api_base="https://api.moonshot.cn/v1",
    )

    assert provider._resolve_api_base_for_model("kimi-coding/k2p5") == "https://api.kimi.com/coding"


def test_kimi_coding_api_base_strips_trailing_v1() -> None:
    provider = LiteLLMProvider(
        default_model="kimi-coding/k2p5",
        api_base="https://api.kimi.com/coding/v1/",
    )

    assert provider._resolve_api_base_for_model("kimi-coding/k2p5") == "https://api.kimi.com/coding"


def test_build_chat_kwargs_respects_api_base_override() -> None:
    provider = LiteLLMProvider(
        default_model="openai/gpt-4o-mini",
        api_base="https://api.moonshot.cn/v1",
    )

    kwargs = provider._build_chat_kwargs(
        model="anthropic/k2p5",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=16,
        temperature=0.7,
        api_base_override="https://api.kimi.com/coding",
    )

    assert kwargs["api_base"] == "https://api.kimi.com/coding"


def test_provider_init_does_not_mutate_global_provider_env(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    _ = LiteLLMProvider(
        api_key="test-key",
        default_model="openai/gpt-4o-mini",
    )

    assert os.environ.get("OPENAI_API_KEY") is None


def test_provider_init_does_not_set_global_litellm_api_base(monkeypatch) -> None:
    sentinel = "https://global.example.invalid/v1"
    monkeypatch.setattr(litellm, "api_base", sentinel, raising=False)

    _ = LiteLLMProvider(
        api_key="test-key",
        api_base="https://provider.example.invalid/v1",
        default_model="openai/gpt-4o-mini",
    )

    assert getattr(litellm, "api_base", None) == sentinel
