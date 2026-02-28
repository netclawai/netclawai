"""
Local LLM Provider — Run your own model, compete with your own brain.

Supports any OpenAI-compatible local endpoint:
  - Ollama (http://localhost:11434)
  - llama.cpp server (http://localhost:8080)
  - vLLM (http://localhost:8000)
  - LM Studio (http://localhost:1234)
  - text-generation-webui (http://localhost:5000)
  - Any custom endpoint with /v1/chat/completions

This is where the real competition happens: fine-tune a model
for specific Battle categories and beat the API-based agents.
"""

import ipaddress
import time
import logging
import urllib.parse

import httpx

from netclaw.providers.base import BaseLLMProvider, LLMResponse

logger = logging.getLogger("netclaw.provider.local")

_BLOCKED_HOSTS = {"169.254.169.254", "metadata.google.internal", "100.100.100.200"}
_ALLOWED_LOCAL_IPS = {
    ipaddress.ip_address("127.0.0.1"),
    ipaddress.ip_address("::1"),
}


def _validate_local_url(url: str) -> str:
    """Validate base_url to prevent SSRF — allow only localhost and public IPs."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Invalid URL scheme: {parsed.scheme}")
    hostname = parsed.hostname or ""
    if hostname in _BLOCKED_HOSTS:
        raise ValueError(f"Blocked host: {hostname}")
    try:
        addr = ipaddress.ip_address(hostname)
        # Check IPv4-mapped IPv6 (e.g., ::ffff:169.254.169.254)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        if addr not in _ALLOWED_LOCAL_IPS:
            if addr.is_private or addr.is_link_local or addr.is_reserved:
                raise ValueError(f"Blocked private/internal IP: {addr}")
    except ValueError as e:
        if "Blocked" in str(e):
            raise
        # hostname is a DNS name — allow (e.g., "localhost", custom hostname)
        # Blocked cloud metadata hostnames already caught above
    return url


# Known local server defaults
PRESETS = {
    "ollama": {
        "base_url": "http://localhost:11434/v1/chat/completions",
        "model": "llama3.2:latest",
    },
    "llamacpp": {
        "base_url": "http://localhost:8080/v1/chat/completions",
        "model": "llama-3.1-8b",
    },
    "vllm": {
        "base_url": "http://localhost:8000/v1/chat/completions",
        "model": "meta-llama/Llama-3.1-8B-Instruct",
    },
    "lmstudio": {
        "base_url": "http://localhost:1234/v1/chat/completions",
        "model": "llama-3.1-8b-instruct",
    },
    "custom": {
        "base_url": "http://localhost:8080/v1/chat/completions",
        "model": "local",
    },
}


class LocalProvider(BaseLLMProvider):
    """
    Local LLM provider for self-hosted models.

    Connects to any OpenAI-compatible API endpoint running locally.
    Zero API cost — you only pay for electricity.

    Usage in config.json:
        {
            "provider": "local",
            "base_url": "http://localhost:11434/v1/chat/completions",
            "model": "llama3.1:70b",
            "preset": "ollama"
        }
    """

    name = "local"

    def __init__(
        self,
        api_key: str = "not-needed",
        model: str = "",
        base_url: str = "",
        preset: str = "ollama",
        timeout: int = 120,
        **kwargs,
    ):
        # Resolve preset
        preset_config = PRESETS.get(preset, PRESETS["custom"])
        resolved_url = base_url or preset_config["base_url"]
        resolved_url = _validate_local_url(resolved_url)
        resolved_model = model or preset_config["model"]

        super().__init__(api_key=api_key, model=resolved_model, **kwargs)
        self.base_url = resolved_url
        self.preset = preset
        self.timeout = timeout

        logger.info(
            f"Local provider initialized | "
            f"preset={preset} | "
            f"url={self.base_url} | "
            f"model={self.model} | "
            f"timeout={timeout}s"
        )

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
            "stream": False,
        }
        for key in ("top_p", "presence_penalty", "frequency_penalty", "seed"):
            if key in kwargs and kwargs[key] is not None:
                payload[key] = kwargs[key]

        headers = {"Content-Type": "application/json"}
        # Some local servers need an auth header even if it's not used
        if self._api_key and self._api_key != "not-needed":
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.base_url,
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise ConnectionError(
                    f"Model '{self.model}' not found on {self.base_url}. "
                    f"Pull it first: ollama pull {self.model}"
                )
            raise
        except httpx.ConnectError:
            raise ConnectionError(
                f"Cannot connect to local LLM at {self.base_url}. "
                f"Is your model server running? "
                f"Try: ollama serve && ollama run {self.model}"
            )
        except httpx.TimeoutException:
            raise TimeoutError(
                f"Local LLM at {self.base_url} timed out after {self.timeout}s. "
                f"Try a smaller model or increase timeout in config."
            )

        latency = (time.monotonic() - start) * 1000
        self._track_latency(latency)

        # Parse response — handle both OpenAI and Ollama formats
        if "choices" in data:
            # OpenAI-compatible format
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)
        elif "message" in data:
            # Ollama native format (fallback)
            text = data["message"].get("content", "")
            input_tokens = data.get("prompt_eval_count", 0)
            output_tokens = data.get("eval_count", 0)
        else:
            raise ValueError(f"Unknown response format from {self.base_url}: {list(data.keys())}")

        return LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            latency_ms=latency,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=0.0,  # Local = free
        )

    async def health_check(self) -> bool:
        """Check if the local LLM server is running."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                # Try a minimal request
                response = await client.post(
                    self.base_url,
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 5,
                    },
                    headers={"Content-Type": "application/json"},
                )
                return response.status_code == 200
        except Exception:
            return False
