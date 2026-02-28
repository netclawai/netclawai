"""
Google Gemini Provider — Gemini models for NetClaw Battles.

Uses Google's OpenAI-compatible endpoint for seamless integration.
Gemini offers a generous free tier (1M tokens) making it ideal
for getting started with NetClaw at zero cost.
"""

import time
import logging
import httpx

from netclaw.providers.base import BaseLLMProvider, LLMResponse

logger = logging.getLogger("netclaw.provider.gemini")


class GeminiProvider(BaseLLMProvider):
    """Google Gemini API provider via OpenAI-compatible endpoint."""

    name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-2.0-flash", **kwargs):
        super().__init__(api_key, model, **kwargs)
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 2048,
        temperature: float = 0.7,
        **kwargs,
    ) -> LLMResponse:
        start = time.monotonic()

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        for key in ("top_p", "presence_penalty", "frequency_penalty", "seed"):
            if key in kwargs and kwargs[key] is not None:
                payload[key] = kwargs[key]

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                self.base_url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
            self._check_rate_limit(response, self.name)
            response.raise_for_status()
            data = response.json()

        latency = (time.monotonic() - start) * 1000
        self._track_latency(latency)

        choice = data["choices"][0]
        usage = data.get("usage", {})

        return LLMResponse(
            text=choice["message"]["content"],
            provider=self.name,
            model=self.model,
            latency_ms=latency,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )
