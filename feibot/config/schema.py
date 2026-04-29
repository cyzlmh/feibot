"""Configuration schema using Pydantic."""

from pathlib import Path

from pydantic import BaseModel, Field
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


class AgentDefaults(BaseModel):
    """Default agent configuration."""
    model: str = Field(...)
    provider: str = "auto"  # provider name or auto-detection
    max_tokens: int = 8192
    context_window_tokens: int = 65_536
    temperature: float = 0.7
    max_tool_iterations: int = 100
    max_consecutive_tool_errors: int = 10
    memory_window: int = 50
    reasoning_effort: str | None = None
    disable_tools: bool = False
    disable_skills: bool = False
    disable_long_term_memory: bool = False


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
    azure_openai: ProviderConfig = Field(default_factory=ProviderConfig)
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)  # 阿里云通义千问
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    ollama: ProviderConfig = Field(default_factory=ProviderConfig)
    ovms: ProviderConfig = Field(default_factory=ProviderConfig)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    mistral: ProviderConfig = Field(default_factory=ProviderConfig)
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API gateway
    siliconflow: ProviderConfig = Field(default_factory=ProviderConfig)
    volcengine: ProviderConfig = Field(default_factory=ProviderConfig)
    volcengine_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)
    byteplus: ProviderConfig = Field(default_factory=ProviderConfig)
    byteplus_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)
    openai_codex: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenAI Codex (OAuth)
    github_copilot: ProviderConfig = Field(default_factory=ProviderConfig)


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


class ToolsConfig(BaseModel):
    """Tools configuration."""
    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    writable_dirs: list[str] = Field(default_factory=list)
    allowed_hosts: list[str] = Field(default_factory=list)
    disabled_tools: list[str] = Field(default_factory=list)  # List of tool names to disable (e.g., ["feishu_doc", "feishu_wiki"])


class SkillsSourcesConfig(BaseModel):
    """Configured skill sources."""
    my: str = ""  # local path to personal skills repo


class SkillsConfig(BaseModel):
    """Skill runtime environment configuration."""
    env: dict[str, str] = Field(default_factory=dict)
    sources: SkillsSourcesConfig = Field(default_factory=SkillsSourcesConfig)


class MadameConfig(BaseModel):
    """Madame control-plane configuration."""

    enabled: bool = False
    runtime_id: str = "madame"
    registry_path: str = "madame/agents_registry.json"
    manage_script: str = ""
    base_dir_template: str = ""
    backup_dir: str = ""
    enforce_isolation: bool = False


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
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    madame: MadameConfig = Field(default_factory=MadameConfig)

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
        from feibot.providers.registry import PROVIDERS, find_by_name

        forced = self.agents.defaults.provider
        if forced != "auto":
            spec = find_by_name(forced)
            if spec:
                p = getattr(self.providers, spec.name, None)
                return (p, spec.name) if p else (None, None)
            return None, None

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
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        # Match by keyword (order follows PROVIDERS registry)
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and any(_kw_matches(kw) for kw in spec.keywords):
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        # Local provider fallback for plain model names (e.g. llama3.2 on ollama).
        local_fallback: tuple[ProviderConfig, str] | None = None
        for spec in PROVIDERS:
            if not spec.is_local:
                continue
            p = getattr(self.providers, spec.name, None)
            if not (p and p.api_base):
                continue
            if spec.detect_by_base_keyword and spec.detect_by_base_keyword in p.api_base:
                return p, spec.name
            if local_fallback is None:
                local_fallback = (p, spec.name)
        if local_fallback:
            return local_fallback

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
        """Get API base URL for the given model. Applies provider defaults when available."""
        from feibot.providers.registry import find_by_name
        p, name = self._match_provider(model)
        if p and p.api_base:
            return p.api_base
        if name:
            spec = find_by_name(name)
            if spec and spec.default_api_base:
                return spec.default_api_base
        return None

    model_config: SettingsConfigDict = {
        "env_prefix": "FEIBOT_",
        "env_nested_delimiter": "__",
    }
