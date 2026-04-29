from feibot.config.schema import Config


def _build_config(model: str, providers: dict | None = None) -> Config:
    return Config.model_validate(
        {
            "name": "test",
            "paths": {"workspace": "./workspace", "sessions": "./sessions"},
            "agents": {"defaults": {"model": model}},
            "providers": providers or {},
        }
    )


def test_get_api_base_returns_default_for_standard_provider() -> None:
    config = _build_config(
        model="moonshot/kimi-k2.5",
        providers={"moonshot": {"api_key": "sk-test"}},
    )

    assert config.get_provider_name() == "moonshot"
    assert config.get_api_base() == "https://api.moonshot.ai/v1"


def test_get_api_base_prefers_explicit_provider_value() -> None:
    config = _build_config(
        model="moonshot/kimi-k2.5",
        providers={
            "moonshot": {
                "api_key": "sk-test",
                "api_base": "https://api.moonshot.cn/v1",
            }
        },
    )

    assert config.get_api_base() == "https://api.moonshot.cn/v1"
