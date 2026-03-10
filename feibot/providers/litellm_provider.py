"""LiteLLM provider implementation for multi-provider support."""

import asyncio
import json
import os
import random
from typing import Any

import litellm
from litellm import acompletion
from loguru import logger

from feibot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from feibot.providers.registry import find_by_model, find_gateway


class LiteLLMProvider(LLMProvider):
    """
    LLM provider using LiteLLM for multi-provider support.
    
    Supports OpenRouter, Anthropic, OpenAI, Gemini, MiniMax, and many other providers through
    a unified interface.  Provider-specific logic is driven by the registry
    (see providers/registry.py) — no if-elif chains needed here.
    """

    RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}
    DEFAULT_MAX_RETRIES = 4
    DEFAULT_RETRY_BASE_DELAY_SEC = 0.75
    DEFAULT_RETRY_MAX_DELAY_SEC = 8.0
    KIMI_CODING_MODEL_PREFIX = "kimi-coding/"
    KIMI_CODING_DEFAULT_BASE = "https://api.kimi.com/coding"
    KIMI_CODING_DEFAULT_MODEL = "k2p5"
    KIMI_CODING_MODEL_ALIASES = {
        "kimi-for-coding": "k2p5",
    }
    
    def __init__(
        self, 
        api_key: str | None = None, 
        api_base: str | None = None,
        default_model: str = "anthropic/claude-opus-4-5",
        fallback_model: str | None = None,
        llm_policy: Any | None = None,
        extra_headers: dict[str, str] | None = None,
        provider_name: str | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.fallback_model = fallback_model.strip() if fallback_model and fallback_model.strip() else None
        self._llm_policy = self._coerce_policy(llm_policy)
        self.extra_headers = extra_headers or {}
        
        # Detect gateway / local deployment.
        # provider_name (from config key) is the primary signal;
        # api_key / api_base are fallback for auto-detection.
        self._gateway = find_gateway(provider_name, api_key, api_base)
        
        # Configure environment variables
        if api_key:
            self._setup_env(api_key, api_base, default_model)
        
        if api_base:
            litellm.api_base = api_base
        
        # Disable LiteLLM logging noise
        litellm.suppress_debug_info = True
        # Drop unsupported parameters for providers (e.g., gpt-5 rejects some params)
        litellm.drop_params = True
    
    def _setup_env(self, api_key: str, api_base: str | None, model: str) -> None:
        """Set environment variables based on detected provider."""
        spec = self._gateway or find_by_model(model)
        if not spec:
            return
        if not spec.env_key:
            # OAuth/provider-only specs (for example: openai_codex)
            return

        # Gateway/local overrides existing env; standard provider doesn't
        if self._gateway:
            os.environ[spec.env_key] = api_key
        else:
            os.environ.setdefault(spec.env_key, api_key)

        # Resolve env_extras placeholders:
        #   {api_key}  → user's API key
        #   {api_base} → user's api_base, falling back to spec.default_api_base
        effective_base = api_base or spec.default_api_base
        for env_name, env_val in spec.env_extras:
            resolved = env_val.replace("{api_key}", api_key)
            resolved = resolved.replace("{api_base}", effective_base)
            os.environ.setdefault(env_name, resolved)
    
    def _resolve_model(self, model: str) -> str:
        """Resolve model name by applying provider/gateway prefixes."""
        if self._gateway:
            # Gateway mode: apply gateway prefix, skip provider-specific prefixes
            prefix = self._gateway.litellm_prefix
            if self._gateway.strip_model_prefix:
                model = model.split("/")[-1]
            if prefix and not model.startswith(f"{prefix}/"):
                model = f"{prefix}/{model}"
            return model

        if self._is_kimi_coding_model_ref(model):
            model_id = self._normalize_kimi_coding_model_id(model)
            return f"anthropic/{model_id}"
        
        # Standard mode: auto-prefix for known providers
        spec = find_by_model(model)
        if spec and spec.litellm_prefix:
            if not any(model.startswith(s) for s in spec.skip_prefixes):
                model = f"{spec.litellm_prefix}/{model}"
        
        return model

    @classmethod
    def _is_kimi_coding_model_ref(cls, model: str | None) -> bool:
        if not model:
            return False
        return model.lower().startswith(cls.KIMI_CODING_MODEL_PREFIX)

    @classmethod
    def _normalize_kimi_coding_model_id(cls, model: str) -> str:
        suffix = model.split("/", 1)[1].strip() if "/" in model else model.strip()
        if not suffix:
            return cls.KIMI_CODING_DEFAULT_MODEL
        return cls.KIMI_CODING_MODEL_ALIASES.get(suffix.lower(), suffix)

    @classmethod
    def _normalize_kimi_coding_api_base(cls, api_base: str) -> str:
        base = api_base.strip().rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        return base or cls.KIMI_CODING_DEFAULT_BASE

    def _resolve_api_base_for_model(self, raw_model: str | None) -> str | None:
        if not self._is_kimi_coding_model_ref(raw_model):
            return self.api_base

        base = (self.api_base or "").strip()
        if not base:
            return self.KIMI_CODING_DEFAULT_BASE

        lower = base.lower()
        if "api.moonshot.cn" in lower or "api.moonshot.ai" in lower:
            return self.KIMI_CODING_DEFAULT_BASE

        return self._normalize_kimi_coding_api_base(base)
    
    def _apply_model_overrides(self, model: str, kwargs: dict[str, Any]) -> None:
        """Apply model-specific parameter overrides from the registry."""
        model_lower = model.lower()
        spec = find_by_model(model)
        if spec:
            for pattern, overrides in spec.model_overrides:
                if pattern in model_lower:
                    kwargs.update(overrides)
                    return

    @classmethod
    def _to_jsonable(cls, value: Any) -> Any:
        """Convert LiteLLM/Pydantic response objects into plain JSON-safe data."""
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(k): cls._to_jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._to_jsonable(v) for v in value]

        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return cls._to_jsonable(model_dump())
            except Exception:
                pass

        to_dict = getattr(value, "dict", None)
        if callable(to_dict):
            try:
                return cls._to_jsonable(to_dict())
            except Exception:
                pass

        if hasattr(value, "__dict__"):
            return {
                str(k): cls._to_jsonable(v)
                for k, v in vars(value).items()
                if not str(k).startswith("_")
            }

        return str(value)

    @staticmethod
    def _coerce_policy(policy: Any) -> dict[str, Any]:
        """Normalize policy object into a plain dict."""
        if policy is None:
            return {}
        if isinstance(policy, dict):
            return policy
        model_dump = getattr(policy, "model_dump", None)
        if callable(model_dump):
            try:
                data = model_dump()
                if isinstance(data, dict):
                    return data
            except Exception:
                return {}
        return {}

    def _policy_section(self, key: str) -> dict[str, Any]:
        section = self._llm_policy.get(key, {})
        return section if isinstance(section, dict) else {}

    @staticmethod
    def _parse_bool(value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        if not isinstance(value, str):
            return None
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return None

    def _retryable_status_codes(self) -> set[int]:
        retry_policy = self._policy_section("retry")
        from_policy = retry_policy.get("retryable_status_codes")
        if isinstance(from_policy, list):
            parsed = {int(v) for v in from_policy if isinstance(v, int) or (isinstance(v, str) and v.isdigit())}
            if parsed:
                return parsed

        raw = os.getenv("FEIBOT_LLM_RETRYABLE_STATUS_CODES")
        if raw:
            parsed = {
                int(part.strip())
                for part in raw.split(",")
                if part.strip().isdigit()
            }
            if parsed:
                return parsed

        return set(self.RETRYABLE_STATUS_CODES)

    def _retry_settings(self) -> tuple[int, float, float]:
        """Read retry settings with precedence: config policy > env > defaults."""
        retry_policy = self._policy_section("retry")

        max_retries = self.DEFAULT_MAX_RETRIES
        base_delay = self.DEFAULT_RETRY_BASE_DELAY_SEC
        max_delay = self.DEFAULT_RETRY_MAX_DELAY_SEC

        from_policy_retries = retry_policy.get("max_retries")
        if isinstance(from_policy_retries, int):
            max_retries = max(0, from_policy_retries)
        else:
            raw_max_retries = os.getenv("FEIBOT_LLM_MAX_RETRIES")
            if raw_max_retries is not None:
                try:
                    max_retries = max(0, int(raw_max_retries))
                except ValueError:
                    pass

        from_policy_base = retry_policy.get("base_delay_sec")
        if isinstance(from_policy_base, (int, float)):
            base_delay = max(0.0, float(from_policy_base))
        else:
            raw_base_delay = os.getenv("FEIBOT_LLM_RETRY_BASE_DELAY_SEC")
            if raw_base_delay is not None:
                try:
                    base_delay = max(0.0, float(raw_base_delay))
                except ValueError:
                    pass

        from_policy_max = retry_policy.get("max_delay_sec")
        if isinstance(from_policy_max, (int, float)):
            max_delay = max(0.0, float(from_policy_max))
        else:
            raw_max_delay = os.getenv("FEIBOT_LLM_RETRY_MAX_DELAY_SEC")
            if raw_max_delay is not None:
                try:
                    max_delay = max(0.0, float(raw_max_delay))
                except ValueError:
                    pass

        if max_delay < base_delay:
            max_delay = base_delay

        return max_retries, base_delay, max_delay

    def _fallback_settings(self) -> tuple[bool, bool]:
        """Read fallback settings with precedence: config policy > env > defaults."""
        fallback_policy = self._policy_section("fallback")
        enabled = True
        retryable_only = True

        from_policy_enabled = fallback_policy.get("enabled")
        if isinstance(from_policy_enabled, bool):
            enabled = from_policy_enabled
        else:
            raw_enabled = os.getenv("FEIBOT_LLM_FALLBACK_ENABLED")
            parsed_enabled = self._parse_bool(raw_enabled)
            if parsed_enabled is not None:
                enabled = parsed_enabled

        from_policy_retryable_only = fallback_policy.get("retryable_errors_only")
        if isinstance(from_policy_retryable_only, bool):
            retryable_only = from_policy_retryable_only
        else:
            raw_retryable_only = os.getenv("FEIBOT_LLM_FALLBACK_RETRYABLE_ONLY")
            parsed_retryable_only = self._parse_bool(raw_retryable_only)
            if parsed_retryable_only is not None:
                retryable_only = parsed_retryable_only

        return enabled, retryable_only

    def _reasoning_effort(self) -> str | None:
        """Resolve optional reasoning effort hint for capable models/providers."""
        reasoning_policy = self._policy_section("reasoning")
        policy_effort = reasoning_policy.get("effort")
        if isinstance(policy_effort, str) and policy_effort.strip():
            return policy_effort.strip()

        raw = os.getenv("FEIBOT_REASONING_EFFORT", "").strip()
        return raw or None

    def _extract_status_code(self, error: Exception) -> int | None:
        """Extract HTTP status code from provider exception when available."""
        status_code = getattr(error, "status_code", None)
        if isinstance(status_code, int):
            return status_code

        response = getattr(error, "response", None)
        if response is not None:
            response_status = getattr(response, "status_code", None)
            if isinstance(response_status, int):
                return response_status
        return None

    def _is_retryable_error(self, error: Exception) -> bool:
        """Decide whether this exception is safe to retry."""
        status_code = self._extract_status_code(error)
        if status_code in self._retryable_status_codes():
            return True

        retryable_error_names = {
            "RateLimitError",
            "Timeout",
            "TimeoutError",
            "APIConnectionError",
            "ServiceUnavailableError",
            "InternalServerError",
        }
        return error.__class__.__name__ in retryable_error_names

    def _retry_delay(self, attempt: int, base_delay: float, max_delay: float) -> float:
        """Exponential backoff with small jitter."""
        delay = min(max_delay, base_delay * (2 ** attempt))
        jitter = random.uniform(0.0, min(0.4, delay * 0.2))
        return delay + jitter

    def _build_chat_kwargs(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        temperature: float,
        api_base_override: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            # We own retry behavior below to keep it predictable across providers.
            "num_retries": 0,
            "max_retries": 0,
        }

        # Apply model-specific overrides (e.g. kimi-k2.5 temperature)
        self._apply_model_overrides(model, kwargs)

        # Pass api_key directly — more reliable than env vars alone
        if self.api_key:
            kwargs["api_key"] = self.api_key

        # Pass api_base for custom endpoints
        effective_api_base = api_base_override if api_base_override is not None else self.api_base
        if effective_api_base:
            kwargs["api_base"] = effective_api_base

        # Pass extra headers (e.g. APP-Code for AiHubMix)
        if self.extra_headers:
            kwargs["extra_headers"] = self.extra_headers

        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        return kwargs

    async def _call_with_retries(
        self,
        kwargs: dict[str, Any],
        max_retries: int,
        base_delay: float,
        max_delay: float,
    ) -> tuple[Any | None, Exception | None]:
        last_error: Exception | None = None
        model_name = str(kwargs.get("model", "unknown"))

        for attempt in range(max_retries + 1):
            try:
                return await acompletion(**kwargs), None
            except Exception as e:
                last_error = e
                if attempt >= max_retries or not self._is_retryable_error(e):
                    break

                wait_sec = self._retry_delay(
                    attempt=attempt,
                    base_delay=base_delay,
                    max_delay=max_delay,
                )
                logger.warning(
                    "LLM call failed ({err}) model={model}; retry {retry}/{total} in {wait:.2f}s",
                    err=e.__class__.__name__,
                    model=model_name,
                    retry=attempt + 1,
                    total=max_retries,
                    wait=wait_sec,
                )
                await asyncio.sleep(wait_sec)

        return None, last_error
    
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """
        Send a chat completion request via LiteLLM.
        
        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions in OpenAI format.
            model: Model identifier (e.g., 'anthropic/claude-sonnet-4-5').
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
        
        Returns:
            LLMResponse with content and/or tool calls.
        """
        primary_raw_model = model or self.default_model
        primary_model = self._resolve_model(primary_raw_model)
        fallback_raw_model = self.fallback_model if self.fallback_model else None
        fallback_model = self._resolve_model(fallback_raw_model) if fallback_raw_model else None
        reasoning_effort = self._reasoning_effort()

        max_retries, base_delay, max_delay = self._retry_settings()
        primary_kwargs = self._build_chat_kwargs(
            model=primary_model,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            api_base_override=self._resolve_api_base_for_model(primary_raw_model),
            reasoning_effort=reasoning_effort,
        )
        response, primary_error = await self._call_with_retries(
            kwargs=primary_kwargs,
            max_retries=max_retries,
            base_delay=base_delay,
            max_delay=max_delay,
        )
        if response is not None:
            return self._parse_response(response, requested_model=primary_model)

        last_error = primary_error

        fallback_enabled, retryable_only = self._fallback_settings()
        can_fallback = (
            fallback_enabled
            and
            fallback_model
            and fallback_model != primary_model
            and primary_error is not None
            and (self._is_retryable_error(primary_error) if retryable_only else True)
        )
        if can_fallback:
            logger.warning(
                "Primary model failed after retries; switching fallback model {fallback}",
                fallback=fallback_model,
            )
            fallback_kwargs = self._build_chat_kwargs(
                model=fallback_model,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
                api_base_override=self._resolve_api_base_for_model(fallback_raw_model),
                reasoning_effort=reasoning_effort,
            )
            response, fallback_error = await self._call_with_retries(
                kwargs=fallback_kwargs,
                max_retries=max_retries,
                base_delay=base_delay,
                max_delay=max_delay,
            )
            if response is not None:
                return self._parse_response(response, requested_model=fallback_model)
            last_error = fallback_error or primary_error

        # Return error as content for graceful handling.
        attempted_models = [primary_model]
        if can_fallback and fallback_model:
            attempted_models.append(fallback_model)
        return LLMResponse(
            content=f"Error calling LLM: {str(last_error) if last_error else 'unknown error'}",
            finish_reason="error",
            model=attempted_models[-1] if attempted_models else None,
            provider_payload={
                "attempted_models": attempted_models,
                "error_type": last_error.__class__.__name__ if last_error else None,
                "error": str(last_error) if last_error else "unknown error",
            },
        )
    
    def _parse_response(self, response: Any, *, requested_model: str | None = None) -> LLMResponse:
        """Parse LiteLLM response into our standard format."""
        choice = response.choices[0]
        message = choice.message
        
        tool_calls = []
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                # Parse arguments from JSON string if needed
                args = tc.function.arguments
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"raw": args}
                
                tool_calls.append(ToolCallRequest(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                ))
        
        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        
        reasoning_content = getattr(message, "reasoning_content", None)
        response_model = getattr(response, "model", None)
        provider_payload = {
            "requested_model": requested_model,
            "response_model": str(response_model) if response_model else None,
            "message": self._to_jsonable(message),
            "finish_reason": choice.finish_reason or "stop",
            "usage": usage or None,
        }
        
        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
            reasoning_content=reasoning_content,
            model=str(response_model or requested_model) if (response_model or requested_model) else None,
            provider_payload={k: v for k, v in provider_payload.items() if v is not None},
        )
    
    def get_default_model(self) -> str:
        """Get the default model."""
        return self.default_model
