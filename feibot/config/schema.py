"""Configuration schema using Pydantic."""

from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class FeishuConfig(BaseModel):
    """Feishu/Lark channel configuration using WebSocket long connection."""
    enabled: bool = False
    app_id: str = ""  # App ID from Feishu Open Platform
    app_secret: str = ""  # App Secret from Feishu Open Platform
    encrypt_key: str = ""  # Encrypt Key for event subscription (optional)
    verification_token: str = ""  # Verification Token for event subscription (optional)
    allow_from: list[str] = Field(
        default_factory=list
    )  # Allowed open_ids; legacy "open_id:phone" entries are normalized.
    wiki_space_id: str = ""  # Default enterprise wiki space ID for new docs
    wiki_parent_node_token: str = ""  # Optional default parent wiki node token
    doc_write_auto_chunk_threshold_chars: int = 6000  # 0 disables auto switch for write/append


class ChannelsConfig(BaseModel):
    """Configuration for Feishu channel behavior."""
    send_progress: bool = True  # stream agent's text progress to the channel
    send_tool_hints: bool = True  # stream tool-call hints (e.g. read_file("…"))
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)


class RetryPolicyConfig(BaseModel):
    """Retry behavior for LLM calls."""
    max_retries: int = 4
    base_delay_sec: float = 0.75
    max_delay_sec: float = 8.0
    retryable_status_codes: list[int] = Field(default_factory=lambda: [408, 409, 429, 500, 502, 503, 504])


class FallbackPolicyConfig(BaseModel):
    """Fallback model behavior when primary call fails."""
    enabled: bool = True
    retryable_errors_only: bool = True


class LLMCallPolicyConfig(BaseModel):
    """Top-level LLM calling policy."""
    retry: RetryPolicyConfig = Field(default_factory=RetryPolicyConfig)
    fallback: FallbackPolicyConfig = Field(default_factory=FallbackPolicyConfig)


class AgentDefaults(BaseModel):
    """Default agent configuration."""
    model: str = Field(...)
    fallback_model: str | None = None
    max_tokens: int = 8192
    temperature: float = 0.7
    max_tool_iterations: int = 20
    memory_window: int = 50
    llm_policy: LLMCallPolicyConfig = Field(default_factory=LLMCallPolicyConfig)


class AgentsConfig(BaseModel):
    """Agent configuration."""
    defaults: AgentDefaults = Field(...)


class ProviderConfig(BaseModel):
    """LLM provider configuration."""
    api_key: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)


class ProvidersConfig(BaseModel):
    """Configuration for LLM providers."""
    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # Any OpenAI-compatible endpoint
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)  # 阿里云通义千问
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API gateway
    openai_codex: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenAI Codex (OAuth)


class GatewayConfig(BaseModel):
    """Gateway/server configuration."""
    host: str = "0.0.0.0"
    port: int = 18790
    heartbeat_target: str = ""  # Optional override, format: "channel:chat_id"


class WebSearchConfig(BaseModel):
    """Web search tool configuration."""
    api_key: str = ""  # Tavily API key
    max_results: int = 5


class WebToolsConfig(BaseModel):
    """Web tools configuration."""
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class ExecToolConfig(BaseModel):
    """Shell exec tool configuration."""

    timeout: int = 60
    path_append: str = ""
    approval_enabled: bool = True
    approval_risk_level: str = ""  # Empty/none disables approval. dangerous=only dangerous, confirm=confirm+dangerous.
    approval_approvers: list[str] = Field(default_factory=list)  # Empty means requester only

    @field_validator("approval_risk_level", mode="before")
    @classmethod
    def _validate_approval_risk_level(cls, value: object) -> str:
        level = str(value or "").strip().lower()
        if level in {"text", "feishu_card"}:
            return "confirm"
        if level in {"", "none", "dangerous", "confirm"}:
            return level
        raise ValueError("approval risk level must be one of: none, dangerous, confirm")


class ToolsConfig(BaseModel):
    """Tools configuration."""
    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    restrict_to_workspace: bool = False  # If true, restrict all tool access to workspace directory
    allowed_dirs: list[str] = Field(default_factory=list)  # Additional directories that are allowed when restrict_to_workspace is True
    disabled_tools: list[str] = Field(default_factory=list)  # List of tool names to disable (e.g., ["feishu_doc", "feishu_wiki"])


class PathsConfig(BaseModel):
    """Filesystem paths for runtime data."""
    workspace: str = Field(...)
    sessions: str = Field(...)


class Config(BaseSettings):
    """Root configuration for feibot."""
    name: str = Field(...)
    paths: PathsConfig = Field(...)
    agents: AgentsConfig = Field(...)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)

    def resolve_workspace_path(self, config_path: Path) -> Path:
        """Resolve workspace path relative to config file when needed."""
        workspace = Path(self.paths.workspace).expanduser()
        if workspace.is_absolute():
            return workspace
        return (config_path.parent / workspace).resolve()

    def resolve_sessions_path(self, config_path: Path) -> Path:
        """Resolve sessions path relative to config file when needed."""
        sessions = Path(self.paths.sessions).expanduser()
        if sessions.is_absolute():
            return sessions
        return (config_path.parent / sessions).resolve()

    def _match_provider(self, model: str | None = None) -> tuple["ProviderConfig | None", str | None]:
        """Match provider config and its registry name. Returns (config, spec_name)."""
        from feibot.providers.registry import PROVIDERS
        model_lower = (model or self.agents.defaults.model).lower()
        model_normalized = model_lower.replace("-", "_")
        model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
        normalized_prefix = model_prefix.replace("-", "_")

        def _kw_matches(kw: str) -> bool:
            kw = kw.lower()
            return kw in model_lower or kw.replace("-", "_") in model_normalized

        # Explicit provider prefix wins — prevents openai-codex models
        # from matching the regular OpenAI provider first.
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and model_prefix and normalized_prefix == spec.name:
                if spec.is_oauth or p.api_key:
                    return p, spec.name

        # Match by keyword (order follows PROVIDERS registry)
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and any(_kw_matches(kw) for kw in spec.keywords):
                if spec.is_oauth or p.api_key:
                    return p, spec.name

        # Fallback: gateways first, then others (follows registry order)
        # OAuth providers are NOT valid fallbacks — they require explicit model selection
        for spec in PROVIDERS:
            if spec.is_oauth:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and p.api_key:
                return p, spec.name
        return None, None

    def get_provider(self, model: str | None = None) -> ProviderConfig | None:
        """Get matched provider config (api_key, api_base, extra_headers). Falls back to first available."""
        p, _ = self._match_provider(model)
        return p

    def get_provider_name(self, model: str | None = None) -> str | None:
        """Get the registry name of the matched provider (e.g. "deepseek", "openrouter")."""
        _, name = self._match_provider(model)
        return name

    def get_api_key(self, model: str | None = None) -> str | None:
        """Get API key for the given model. Falls back to first available key."""
        p = self.get_provider(model)
        return p.api_key if p else None

    def get_api_base(self, model: str | None = None) -> str | None:
        """Get API base URL for the given model. Applies default URLs for known gateways."""
        from feibot.providers.registry import find_by_name
        p, name = self._match_provider(model)
        if p and p.api_base:
            return p.api_base
        # Only gateways get a default api_base here. Standard providers
        # (like Moonshot) set their base URL via env vars in _setup_env
        # to avoid polluting the global litellm.api_base.
        if name:
            spec = find_by_name(name)
            if spec and spec.is_gateway and spec.default_api_base:
                return spec.default_api_base
        return None

    model_config: SettingsConfigDict = {
        "env_prefix": "FEIBOT_",
        "env_nested_delimiter": "__",
    }
