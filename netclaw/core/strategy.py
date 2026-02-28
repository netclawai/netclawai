"""
Strategy Engine — Auto-tuning for Battle performance optimization.

Controls LLM parameters (temperature, max_tokens, response style) and
auto-tunes them based on past battle scores. Persists to strategy.json.
"""

import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("netclaw.strategy")

MAX_TUNE_HISTORY = 200
_KNOWN_PARAM_KEYS = {
    "temperature", "max_tokens", "auto_tune", "response_style",
    "creative_params", "updated_at",
}


class Strategy:
    """Mining strategy with auto-tuning. Persists to strategy.json."""

    DEFAULTS = {
        "temperature": 0.7,
        "max_tokens": 2048,
        "auto_tune": True,
        "response_style": "concise",
        "creative_params": {
            "enabled": False,
            "randomize": False,
            "top_p": None,
            "presence_penalty": None,
            "frequency_penalty": None,
        },
    }

    def __init__(self, strategy_dir: Path):
        self.dir = strategy_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = strategy_dir / "strategy.json"
        self.params: dict = dict(self.DEFAULTS)
        self.auto_tune_enabled = True
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path) as f:
                    saved = json.load(f)
                self.params = {**self.DEFAULTS, **saved}
                # Clamp values to safe ranges
                self.params["temperature"] = max(0.0, min(2.0, float(self.params.get("temperature", 0.7))))
                self.params["max_tokens"] = max(1, min(32768, int(self.params.get("max_tokens", 2048))))
                self.auto_tune_enabled = self.params.get("auto_tune", True)
                # Clean unknown keys
                stale_keys = [k for k in self.params if k not in _KNOWN_PARAM_KEYS]
                for k in stale_keys:
                    logger.debug(f"Strategy: removing unknown param key '{k}'")
                    del self.params[k]
            except Exception as e:
                logger.warning(f"Failed to load strategy: {e}")

    def save(self):
        self.params["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp_path = self.path.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(self.params, f, indent=2)
        os.replace(tmp_path, self.path)

    def get_temperature(self) -> float:
        return self.params.get("temperature", 0.7)

    def get_max_tokens(self) -> int:
        return self.params.get("max_tokens", 2048)

    def get_directives(self) -> str:
        style = self.params.get("response_style", "concise")
        return {
            "concise": "Be precise and concise. Prioritize accuracy.",
            "detailed": "Provide thorough responses with examples.",
            "creative": "Be creative and engaging while staying accurate.",
        }.get(style, "Be precise and concise.")

    def get_creative_params(self) -> dict:
        """Get extra LLM params for diverse responses.

        Returns kwargs to forward to the provider. Empty dict if disabled.
        Enable in strategy.json: {"creative_params": {"enabled": true, "randomize": true}}
        """
        cp = self.params.get("creative_params", {})
        if not isinstance(cp, dict) or not cp.get("enabled", False):
            return {}
        if cp.get("randomize", False):
            return {
                "top_p": round(random.uniform(0.85, 1.0), 2),
                "presence_penalty": round(random.uniform(0.0, 0.4), 2),
                "frequency_penalty": round(random.uniform(0.0, 0.3), 2),
            }
        result = {}
        for key in ("top_p", "presence_penalty", "frequency_penalty"):
            val = cp.get(key)
            if val is not None:
                try:
                    result[key] = max(0.0, min(2.0, float(val)))
                except (ValueError, TypeError):
                    logger.warning(f"Invalid creative_params.{key}: {val!r} — skipped")
        return result

    async def auto_tune(self, battle_results: list[dict]):
        """Adjust strategy based on recent Battle results."""
        if not self.auto_tune_enabled or len(battle_results) < 10:
            return

        # Cap to most recent results
        recent = battle_results[-MAX_TUNE_HISTORY:] if len(battle_results) > MAX_TUNE_HISTORY else battle_results
        scores = [r.get("score", 0) for r in recent if "score" in r]
        if not scores:
            return

        avg = sum(scores) / len(scores)

        if avg < 5.0:
            old = self.params["temperature"]
            self.params["temperature"] = (
                max(0.1, old - 0.1) if old > 0.5
                else min(1.0, old + 0.1)
            )
            logger.info(
                f"Auto-tune: temp {old:.1f} → {self.params['temperature']:.1f} "
                f"(avg_score: {avg:.1f})"
            )

        self.save()
