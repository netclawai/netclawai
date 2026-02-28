"""
Base LLM Provider — Abstract interface for remote inference.

All providers must implement this interface. Keeps the agent
decoupled from any specific LLM API.
"""

import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("netclaw.provider")


class RateLimitError(Exception):
    """Raised when a provider returns 429 with rate limit info."""

    def __init__(self, message: str, retry_after: float = 0, tokens_remaining: int = 0):
        super().__init__(message)
        self.retry_after = retry_after
        self.tokens_remaining = tokens_remaining


@dataclass
class LLMResponse:
    """Response from an LLM provider."""
    text: str
    provider: str
    model: str
    latency_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    metadata: dict = field(default_factory=dict)


def _parse_duration(s: str) -> float:
    """Parse Go-style duration string to seconds.

    Handles formats from Groq/OpenAI reset headers:
      '7.66s' -> 7.66
      '2m59.56s' -> 179.56
      '23h18m29.144s' -> 83909.144
      '6ms' -> 0.006
      '0s' -> 0.0
    """
    if not s:
        return 0.0
    total = 0.0
    # Match 'ms' before single 'm' to avoid ambiguity
    for val, unit in re.findall(r'(\d+\.?\d*)(ms|[hms])', s):
        val = float(val)
        if unit == 'h':
            total += val * 3600
        elif unit == 'm':
            total += val * 60
        elif unit == 's':
            total += val
        elif unit == 'ms':
            total += val / 1000
    return total


def _safe_int(v, default=0):
    """Parse int from header value, return default on failure."""
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


class BaseLLMProvider(ABC):
    """Abstract base class for LLM providers."""

    name: str = "base"

    def __init__(self, api_key: str, model: str, **kwargs):
        self._api_key = api_key
        self.model = model
        self.base_url: str = ""
        self._request_count = 0
        self._total_latency = 0.0

    @abstractmethod
    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 2048,
        temperature: float = 0.7,
        **kwargs,
    ) -> LLMResponse:
        """Generate a response from the LLM.

        Extra kwargs forwarded to API (only if provided):
            top_p, presence_penalty, frequency_penalty, seed
        """
        ...

    async def health_check(self) -> bool:
        """Check if the provider is reachable."""
        try:
            response = await self.generate(
                system_prompt="",
                user_prompt="Say 'ok'",
                max_tokens=5,
                temperature=0,
            )
            return bool(response.text)
        except Exception:
            return False

    @property
    def avg_latency_ms(self) -> float:
        if self._request_count == 0:
            return 0
        return self._total_latency / self._request_count

    def __repr__(self):
        return f"{self.__class__.__name__}(model={self.model!r})"

    def _track_latency(self, ms: float):
        self._request_count += 1
        self._total_latency += ms

    @staticmethod
    def _check_rate_limit(response, provider_name: str):
        """Universal rate limit handler for all providers.

        On 200: logs rate limit headers at DEBUG level, warns if nearly exhausted.
        On 429: parses headers + body across all provider formats, raises RateLimitError.

        Supported providers and their 429 formats:
          - Groq/OpenAI: x-ratelimit-* headers + retry-after (Groq only)
          - DeepSeek: may or may not have headers (OpenAI-compatible, not guaranteed)
          - OpenRouter: X-RateLimit-Limit/Remaining/Reset (ms timestamp) + JSON body
          - Gemini: NO headers, retry info in JSON body error.details[].retryDelay
        """
        h = response.headers

        # ── Extract rate limit info from headers (works for Groq/OpenAI/DeepSeek) ──
        # OpenAI-style: x-ratelimit-remaining-requests, x-ratelimit-remaining-tokens
        remaining_req = h.get("x-ratelimit-remaining-requests")
        limit_req = h.get("x-ratelimit-limit-requests")
        remaining_tok = h.get("x-ratelimit-remaining-tokens")
        limit_tok = h.get("x-ratelimit-limit-tokens")

        # OpenRouter-style: X-RateLimit-Remaining (no -requests suffix)
        if remaining_req is None:
            remaining_req = h.get("x-ratelimit-remaining")
            limit_req = h.get("x-ratelimit-limit")

        # ── On success: monitoring logs ──
        if response.status_code == 200:
            if remaining_req is not None or remaining_tok is not None:
                parts = []
                if remaining_req is not None:
                    parts.append(f"req={remaining_req}/{limit_req or '?'}")
                if remaining_tok is not None:
                    parts.append(f"tok={remaining_tok}/{limit_tok or '?'}")
                logger.debug(f"{provider_name} rate limit: {' | '.join(parts)}")

                # Early warning: requests nearly exhausted
                r = _safe_int(remaining_req, -1)
                if 0 <= r <= 5:
                    logger.warning(
                        f"{provider_name} requests nearly exhausted: "
                        f"{remaining_req}/{limit_req}"
                    )
                # Early warning: tokens nearly exhausted
                t = _safe_int(remaining_tok, -1)
                if 0 <= t <= 1000:
                    logger.warning(
                        f"{provider_name} tokens nearly exhausted: "
                        f"{remaining_tok}/{limit_tok}"
                    )
            return

        # ── Not 429? Return (let raise_for_status handle it) ──
        if response.status_code != 429:
            return

        # ══════════════════════════════════════════════════════════
        # 429 HANDLING — extract retry_after from multiple sources
        # ══════════════════════════════════════════════════════════

        retry_after = 0.0
        extra_info = []

        # Source 1: retry-after HTTP header (Groq: always on 429, others: sometimes)
        raw_retry = h.get("retry-after", "")
        if raw_retry:
            try:
                retry_after = float(raw_retry)
            except ValueError:
                pass

        # Source 2: x-ratelimit-reset-requests / reset-tokens (OpenAI/Groq)
        # Format: Go-style duration string like "2m59.56s", "7.66s"
        reset_req = h.get("x-ratelimit-reset-requests", "")
        reset_tok = h.get("x-ratelimit-reset-tokens", "")
        if retry_after == 0 and reset_req:
            retry_after = _parse_duration(reset_req)
        if retry_after == 0 and reset_tok:
            retry_after = _parse_duration(reset_tok)
        if reset_tok:
            extra_info.append(f"reset_tok={reset_tok}")
        elif reset_req:
            extra_info.append(f"reset_req={reset_req}")

        # Source 3: X-RateLimit-Reset (OpenRouter: Unix timestamp in MILLISECONDS)
        reset_generic = h.get("x-ratelimit-reset", "")
        if retry_after == 0 and reset_generic:
            try:
                ts = float(reset_generic)
                if ts > 1_000_000_000_000:  # Unix ms
                    retry_after = max(0, (ts / 1000) - time.time())
                elif ts > 1_000_000_000:  # Unix seconds
                    retry_after = max(0, ts - time.time())
            except ValueError:
                pass

        # Source 4: JSON response body (Gemini + OpenRouter)
        try:
            body = response.json()
            error = body.get("error", {})

            # Gemini: error.details[].retryDelay ("1s", "5s")
            for detail in error.get("details", []):
                if "retryDelay" in detail:
                    delay = _parse_duration(detail["retryDelay"])
                    if delay > 0 and retry_after == 0:
                        retry_after = delay
                # Gemini: quota violation info
                for v in detail.get("violations", []):
                    quota_id = v.get("quotaId", "")
                    if quota_id:
                        extra_info.append(f"quota={quota_id}")

            # OpenRouter: error.metadata.headers
            meta_h = error.get("metadata", {}).get("headers", {})
            if meta_h:
                if remaining_req is None:
                    remaining_req = meta_h.get("X-RateLimit-Remaining")
                    limit_req = meta_h.get("X-RateLimit-Limit")
                reset_meta = meta_h.get("X-RateLimit-Reset", "")
                if reset_meta and retry_after == 0:
                    try:
                        ts = float(reset_meta)
                        if ts > 1_000_000_000_000:
                            retry_after = max(0, (ts / 1000) - time.time())
                    except ValueError:
                        pass
        except Exception:
            pass

        # ── Parse tokens remaining ──
        tokens_left = _safe_int(remaining_tok)

        # ── Build log message ──
        parts = [f"{provider_name} 429 rate limited"]
        if remaining_req is not None:
            parts.append(f"req={remaining_req}/{limit_req or '?'}")
        if remaining_tok is not None:
            parts.append(f"tok={remaining_tok}/{limit_tok or '?'}")
        if retry_after > 0:
            parts.append(f"retry={retry_after:.0f}s")
        parts.extend(extra_info)

        logger.warning(" | ".join(parts))

        raise RateLimitError(
            f"{provider_name} 429: retry in {retry_after:.0f}s, "
            f"tokens left: {tokens_left}",
            retry_after=retry_after,
            tokens_remaining=tokens_left,
        )
