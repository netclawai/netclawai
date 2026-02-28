"""Memory store — persistent learning from past Battle results."""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("netclaw.memory")

MAX_CONTEXT_CACHE_SIZE = 50
MAX_STATS_CATEGORIES = 100
MAX_STATS_PROVIDERS = 50


class MemoryStore:
    """
    File-based memory for learning from scored Battle interactions.
    Stores in JSONL format with MEMORY.md summary. Minimal footprint.
    In-memory caches are bounded with LRU eviction.
    """

    def __init__(self, memory_dir: Path, max_entries: int = 1000):
        self.memory_dir = memory_dir
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.max_entries = max_entries
        self._interactions_file = memory_dir / "interactions.jsonl"
        self._memory_file = memory_dir / "MEMORY.md"
        self._write_lock = asyncio.Lock()
        # Bounded LRU cache for context lookups
        self._context_cache: collections.OrderedDict[str, str] = collections.OrderedDict()

    async def store_interaction(
        self,
        challenge: dict[str, Any],
        response: str,
        provider: str,
        score: float | None = None,
    ) -> None:
        """Store a Battle interaction for future learning."""
        entry = {
            "ts": time.time(),
            "category": challenge.get("category", "unknown"),
            "challenge_hash": hashlib.sha256(str(challenge).encode()).hexdigest()[:12],
            "response_len": len(response),
            "provider": provider,
            "score": score,
        }

        async with self._write_lock:
            with open(self._interactions_file, "a") as f:
                f.write(json.dumps(entry) + "\n")

            await self._rotate_if_needed()

        # Invalidate cache on new data
        self._context_cache.clear()

    async def update_score(self, challenge_hash: int, score: float) -> None:
        """Update score for a past interaction when Battle results come in."""
        if not self._interactions_file.exists():
            return

        async with self._write_lock:
            lines = self._interactions_file.read_text().strip().split("\n")
            updated = []
            for line in lines:
                try:
                    entry = json.loads(line)
                    if entry.get("challenge_hash") == challenge_hash:
                        entry["score"] = score
                    updated.append(json.dumps(entry))
                except json.JSONDecodeError:
                    continue

            tmp_file = self._interactions_file.with_suffix(".tmp")
            tmp_file.write_text("\n".join(updated) + "\n")
            os.replace(tmp_file, self._interactions_file)

        # Invalidate cache on score change
        self._context_cache.clear()

    def get_relevant_context(
        self,
        challenge_type: str = "text",
        limit: int = 3,
    ) -> str:
        """Get relevant context from past Battle performance (LRU cached)."""
        cache_key = f"{challenge_type}:{limit}"
        if cache_key in self._context_cache:
            self._context_cache.move_to_end(cache_key)
            return self._context_cache[cache_key]

        result = self._compute_relevant_context(challenge_type, limit)

        # Store in bounded cache
        self._context_cache[cache_key] = result
        while len(self._context_cache) > MAX_CONTEXT_CACHE_SIZE:
            self._context_cache.popitem(last=False)  # Evict oldest

        return result

    def _compute_relevant_context(
        self,
        challenge_type: str,
        limit: int,
    ) -> str:
        """Compute relevant context from file (internal, uncached)."""
        if self._memory_file.exists():
            return self._memory_file.read_text(encoding="utf-8")[:500]

        if not self._interactions_file.exists():
            return ""

        try:
            lines = self._interactions_file.read_text().strip().split("\n")
            entries = []
            for line in lines[-100:]:
                try:
                    entry = json.loads(line)
                    if (
                        entry.get("score") is not None
                        and entry.get("category") == challenge_type
                    ):
                        entries.append(entry)
                except json.JSONDecodeError:
                    continue

            if not entries:
                return ""

            entries.sort(key=lambda x: x.get("score", 0), reverse=True)
            top = entries[:limit]

            insights = []
            for e in top:
                insights.append(
                    f"- {e['category']}: provider={e['provider']} "
                    f"scored {e['score']:.2f} (len={e['response_len']})"
                )

            return "High-scoring patterns:\n" + "\n".join(insights)

        except Exception as e:
            logger.warning(f"Failed to read memory: {e}")
            return ""

    async def _rotate_if_needed(self) -> None:
        if not self._interactions_file.exists():
            return
        lines = self._interactions_file.read_text().strip().split("\n")
        if len(lines) > self.max_entries:
            keep = lines[len(lines) // 2:]
            tmp_file = self._interactions_file.with_suffix(".tmp")
            tmp_file.write_text("\n".join(keep) + "\n")
            os.replace(tmp_file, self._interactions_file)
            logger.info(f"Rotated memory: {len(lines)} -> {len(keep)} entries")

    def get_stats(self) -> dict[str, Any]:
        if not self._interactions_file.exists():
            return {"entries": 0}

        lines = self._interactions_file.read_text().strip().split("\n")
        scored = 0
        total_score = 0
        categories: dict[str, int] = {}
        providers: dict[str, int] = {}

        for line in lines:
            try:
                entry = json.loads(line)
                if entry.get("score") is not None:
                    scored += 1
                    total_score += entry["score"]
                cat = entry.get("category", "unknown")
                # Cap to prevent unbounded growth
                if cat in categories or len(categories) < MAX_STATS_CATEGORIES:
                    categories[cat] = categories.get(cat, 0) + 1
                p = entry.get("provider", "unknown")
                # Cap to prevent unbounded growth
                if p in providers or len(providers) < MAX_STATS_PROVIDERS:
                    providers[p] = providers.get(p, 0) + 1
            except json.JSONDecodeError:
                continue

        return {
            "entries": len(lines),
            "scored": scored,
            "avg_score": total_score / scored if scored > 0 else 0,
            "categories": categories,
            "providers": providers,
        }
