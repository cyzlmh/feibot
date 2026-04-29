from pathlib import Path

from feibot.cli.commands import _make_provider
from feibot.config.schema import Config
from feibot.providers.openai_codex_provider import OpenAICodexProvider, _strip_model_prefix


def _build_config(model: str, providers: dict | None = None) -> Config:
    return Config.model_validate(
        {
            "name": "test",
            "paths": {"workspace": "./workspace", "sessions": "./sessions"},
            "agents": {"defaults": {"model": model}},
            "providers": providers or {},
        }
    )


def test_config_matches_openai_codex_prefix_before_openai_keyword() -> None:
    config = _build_config(
        model="openai-codex/gpt-5.1-codex",
        providers={
            "openai": {"api_key": "sk-openai-test"},
            "openai_codex": {},
        },
    )

    assert config.get_provider_name() == "openai_codex"


def test_config_matches_openai_codex_with_underscore_prefix() -> None:
    config = _build_config(
        model="openai_codex/gpt-5.1-codex",
        providers={
            "openai": {"api_key": "sk-openai-test"},
            "openai_codex": {},
        },
    )

    assert config.get_provider_name() == "openai_codex"


def test_make_provider_returns_openai_codex_without_api_key() -> None:
    config = _build_config(model="openai-codex/gpt-5.1-codex")

    provider = _make_provider(config, Path("/tmp/config.json"))

    assert isinstance(provider, OpenAICodexProvider)


def test_openai_codex_strip_prefix_supports_hyphen_and_underscore() -> None:
    assert _strip_model_prefix("openai-codex/gpt-5.1-codex") == "gpt-5.1-codex"
    assert _strip_model_prefix("openai_codex/gpt-5.1-codex") == "gpt-5.1-codex"
