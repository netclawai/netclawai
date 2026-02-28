"""
Provider Router — Intelligent routing between LLM providers.

Handles provider selection, fallback on errors, and A/B testing
across different providers/models.
"""

import asyncio
import logging
from typing import Optional

from netclaw.providers.base import BaseLLMProvider, LLMResponse, RateLimitError
from netclaw.providers.groq_provider import GroqProvider
from netclaw.providers.deepseek import DeepSeekProvider
from netclaw.providers.openrouter import OpenRouterProvider
from netclaw.providers.openai_provider import OpenAIProvider
from netclaw.providers.gemini import GeminiProvider
from netclaw.providers.local import LocalProvider

logger = logging.getLogger("netclaw.router")

PROVIDER_MAP = {
    "groq": GroqProvider,
    "deepseek": DeepSeekProvider,
    "openrouter": OpenRouterProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
    "local": LocalProvider,
    "ollama": LocalProvider,
    "llamacpp": LocalProvider,
    "vllm": LocalProvider,
    "lmstudio": LocalProvider,
}


class ProviderRouter:
    """
    Routes inference requests to the best available provider
    with automatic fallback.
    """

    def __init__(
        self,
        primary: BaseLLMProvider,
        fallback: Optional[BaseLLMProvider] = None,
    ):
        self.primary = primary
        self.fallback = fallback
        self.active_provider = primary
        self._error_count = 0
        self._max_errors = 3

    @classmethod
    def from_config(cls, config: dict) -> "ProviderRouter":
        """Create a router from provider config dict."""
        name = config.get("name", "groq")
        api_key = config.get("api_key", "")
        model = config.get("model", "")

        provider_cls = PROVIDER_MAP.get(name, GroqProvider)

        # Extra kwargs for local providers
        extra = {}
        if name in ("local", "ollama", "llamacpp", "vllm", "lmstudio"):
            extra["base_url"] = config.get("base_url", "")
            extra["preset"] = config.get("preset", name)
            extra["timeout"] = config.get("timeout", 120)

        primary = provider_cls(api_key=api_key, model=model, **extra)

        fallback = None
        fb_config = config.get("fallback", {})
        if fb_config.get("name"):
            fb_cls = PROVIDER_MAP.get(fb_config["name"], DeepSeekProvider)
            fb_extra = {}
            if fb_config["name"] in ("local", "ollama", "llamacpp", "vllm", "lmstudio"):
                fb_extra["base_url"] = fb_config.get("base_url", "")
                fb_extra["preset"] = fb_config.get("preset", fb_config["name"])
                fb_extra["timeout"] = fb_config.get("timeout", 120)
            fallback = fb_cls(
                api_key=fb_config.get("api_key", ""),
                model=fb_config.get("model", ""),
                **fb_extra,
            )

        return cls(primary=primary, fallback=fallback)

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 2048,
        temperature: float = 0.7,
        **kwargs,
    ) -> LLMResponse:
        """
        Generate a response with retry + exponential backoff + fallback.

        Retry flow:
          1. Try active provider up to _max_errors times with backoff
          2. If all retries fail and fallback exists, switch to fallback
          3. If no fallback, raise the last error
        """
        last_error = None

        for attempt in range(1, self._max_errors + 1):
            try:
                response = await self.active_provider.generate(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    **kwargs,
                )
                self._error_count = 0
                return response

            except RateLimitError as e:
                last_error = e
                self._error_count = attempt
                logger.warning(
                    f"Provider {self.active_provider.name} rate limited "
                    f"({attempt}/{self._max_errors}): {e}"
                )

                # If retry_after is too long, don't waste time retrying
                if e.retry_after > 30:
                    logger.warning(
                        f"Rate limit reset in {e.retry_after:.0f}s — "
                        f"skipping retries (too long)"
                    )
                    break

                if attempt < self._max_errors:
                    # Use retry_after if available, else exponential backoff
                    delay = e.retry_after if e.retry_after > 0 else min(2 ** attempt, 10)
                    delay = min(delay, 15)  # Cap at 15s per retry
                    logger.info(
                        f"Retrying in {delay:.0f}s "
                        f"(attempt {attempt}/{self._max_errors})"
                    )
                    await asyncio.sleep(delay)

            except Exception as e:
                last_error = e
                self._error_count = attempt
                logger.warning(
                    f"Provider {self.active_provider.name} error "
                    f"({attempt}/{self._max_errors}): {type(e).__name__}"
                )

                if attempt < self._max_errors:
                    delay = min(2 ** attempt, 10)  # 2s, 4s, 8s
                    logger.info(
                        f"Retrying in {delay}s "
                        f"(attempt {attempt}/{self._max_errors})"
                    )
                    await asyncio.sleep(delay)

        # All retries exhausted — try fallback provider
        if self.fallback and self.active_provider != self.fallback:
            logger.info(f"Switching to fallback: {self.fallback.name}")
            self.active_provider = self.fallback
            self._error_count = 0

            return await self.fallback.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                **kwargs,
            )

        # No fallback — reset count for next call and raise
        self._error_count = 0
        raise last_error

    def switch_provider(self, provider_name: str):
        """Manually switch the active provider."""
        if provider_name == self.primary.name:
            self.active_provider = self.primary
        elif self.fallback and provider_name == self.fallback.name:
            self.active_provider = self.fallback
        else:
            logger.warning(f"Unknown provider: {provider_name}")
            return

        self._error_count = 0
        logger.info(f"🔄 Switched to provider: {provider_name}")
