from pathlib import Path

from feibot.cli.commands import _make_provider
from feibot.config.schema import Config
from feibot.providers.anthropic_provider import AnthropicProvider
from feibot.providers.openai_compat_provider import OpenAICompatProvider
from feibot.providers.openai_codex_provider import OpenAICodexProvider


def _build_config(model: str, providers: dict | None = None) -> Config:
    return Config.model_validate(
        {
            "name": "test",
            "paths": {"workspace": "./workspace", "sessions": "./sessions"},
            "agents": {"defaults": {"model": model}},
            "providers": providers or {},
        }
    )


def test_make_provider_returns_anthropic_provider() -> None:
    config = _build_config(
        model="anthropic/claude-sonnet-4-5",
        providers={"anthropic": {"api_key": "sk-ant-test"}},
    )

    provider = _make_provider(config, Path("/tmp/config.json"))
    assert isinstance(provider, AnthropicProvider)


def test_make_provider_returns_openai_compat_provider() -> None:
    config = _build_config(
        model="openai/gpt-4o-mini",
        providers={"openai": {"api_key": "sk-openai-test"}},
    )

    provider = _make_provider(config, Path("/tmp/config.json"))
    assert isinstance(provider, OpenAICompatProvider)


def test_make_provider_returns_openai_codex_without_api_key() -> None:
    config = _build_config(model="openai-codex/gpt-5.1-codex")

    provider = _make_provider(config, Path("/tmp/config.json"))
    assert isinstance(provider, OpenAICodexProvider)
