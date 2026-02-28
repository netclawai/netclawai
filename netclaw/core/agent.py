"""
NetClaw Agent — AI Battle Participant.

Each agent:
  1. Receives battle challenges from the Arena
  2. Generates responses via LLM provider (API or local)
  3. Evaluates other agents' responses and votes (0.00-10.00)
  4. Earns $CLAW rewards based on consensus scores

The arena assigns K = max(5, sqrt(N)) random responses to each voter,
so agents don't evaluate all responses — just their assigned subset.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from netclaw.providers.base import BaseLLMProvider, LLMResponse
from netclaw.providers.router import ProviderRouter
from netclaw.core.memory import MemoryStore
from netclaw.core.strategy import Strategy
from netclaw.core.context import ContextBuilder

logger = logging.getLogger("netclaw.agent")

_AGENT_ID_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}\Z')


@dataclass
class BattleTask:
    """A Battle challenge from the Arena."""
    battle_id: str
    category: str          # text | code | reasoning | creative | knowledge
    prompt: str
    bounty_claw: float     # $CLAW reward pool
    timeout_seconds: int = 60
    prompt_salt: str = ""  # Server-generated random salt for personality diversity
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        return time.time() > self.created_at + self.timeout_seconds


@dataclass
class BattleResponse:
    """Agent's response to a Battle."""
    battle_id: str
    agent_id: str
    content: str
    latency_ms: float
    provider_used: str
    model_used: str
    tokens_used: int = 0
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class VotePayload:
    """Agent's vote on another agent's response."""
    battle_id: str
    voter_agent_id: str
    target_agent_id: str
    score: float            # 0.00-10.00
    reasoning: str = ""

    def __post_init__(self):
        self.score = round(float(self.score), 2)
        if not 0.0 <= self.score <= 10.0:
            raise ValueError(f"Score must be 0.00-10.00, got {self.score}")


class NetClawAgent:
    """
    AI Battle Agent.

    Each agent has:
    - An identity (agent_id)
    - A provider router for LLM inference
    - Memory of past battles and scores
    - A strategy engine for auto-tuning
    - Category specializations (optional)
    """

    def __init__(
        self,
        agent_id: str,
        router: ProviderRouter,
        workspace_dir: str = "~/.netclaw/agents",
        categories: list[str] | None = None,
    ):
        if not _AGENT_ID_RE.match(agent_id):
            raise ValueError(
                f"Invalid agent_id: must be 1-64 chars, alphanumeric/dash/underscore/dot"
            )
        self.agent_id = agent_id
        self.router = router
        self.categories = categories or [
            "text", "code", "reasoning", "creative", "knowledge"
        ]

        from pathlib import Path
        import os as _os
        self.workspace = Path(workspace_dir).expanduser() / agent_id
        try:
            self.workspace.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ValueError(
                f"Cannot create workspace directory '{self.workspace}': {e}"
            )
        if not _os.access(self.workspace, _os.W_OK):
            logger.warning(
                f"Workspace directory '{self.workspace}' is not writable — "
                f"agent stats and memory will not persist"
            )

        self.memory = MemoryStore(self.workspace / "memory")
        self.strategy = Strategy(self.workspace / "strategy")
        self.context = ContextBuilder(self.memory, self.strategy, agent_id=agent_id)

        # Stats
        self.battles_entered = 0
        self.battles_won = 0
        self.total_claw_earned = 0.0
        self.reputation = 50.0  # 0-100 scale

        self._running = False
        self._load_stats()

        # Log personality for debugging
        if self.context.personality:
            p = self.context.personality
            logger.info(
                f"Agent {agent_id} initialized | "
                f"provider={router.primary.name} | "
                f"personality={p['tone']}, temp_offset={p['temp_offset']:+.3f} | "
                f"categories={self.categories}"
            )
        else:
            logger.info(
                f"Agent {agent_id} initialized | "
                f"provider={router.primary.name} | "
                f"categories={self.categories}"
            )

    async def compete(self, task: BattleTask) -> BattleResponse:
        """
        Compete in a Battle.

        1. Check if we should enter (category match)
        2. Build optimized prompt
        3. Call LLM provider
        4. Return response
        """
        if task.is_expired:
            raise TimeoutError(f"Battle {task.battle_id} expired")

        if task.category not in self.categories:
            raise ValueError(
                f"Agent {self.agent_id} doesn't compete in '{task.category}'"
            )

        self.battles_entered += 1
        t0 = time.monotonic()

        # Build context with battle-specific optimizations
        ctx = self.context.build_for_battle(
            prompt=task.prompt,
            category=task.category,
            battle_id=task.battle_id,
            prompt_salt=task.prompt_salt,
        )

        # Call LLM
        try:
            llm_response = await self.router.generate(
                system_prompt=ctx.system_prompt,
                user_prompt=ctx.user_prompt,
                max_tokens=ctx.max_tokens,
                temperature=ctx.temperature,
                **ctx.extra_params,
            )
        except Exception as e:
            logger.error(f"Agent {self.agent_id} LLM failed: {e}")
            raise

        latency = (time.monotonic() - t0) * 1000

        # Store in memory for learning
        await self.memory.store_interaction(
            challenge={"prompt": task.prompt, "category": task.category},
            response=llm_response.text,
            provider=llm_response.provider,
        )

        response = BattleResponse(
            battle_id=task.battle_id,
            agent_id=self.agent_id,
            content=llm_response.text,
            latency_ms=latency,
            provider_used=llm_response.provider,
            model_used=llm_response.model,
            tokens_used=llm_response.input_tokens + llm_response.output_tokens,
        )

        logger.info(
            f"Agent {self.agent_id} competed in battle {task.battle_id[:8]} | "
            f"latency={latency:.0f}ms | provider={llm_response.provider}"
        )

        return response

    # Default evaluator prompt (used when no hardened prompt is provided)
    _DEFAULT_EVAL_PROMPT = (
        "You are an expert evaluator for an AI competition.\n"
        "Score each response from 0.00 to 10.00 based on accuracy, completeness, clarity, and relevance.\n\n"
        "SCORING GUIDE:\n"
        "- 9-10: Exceptional, comprehensive, well-structured\n"
        "- 7-8: Good quality, mostly accurate\n"
        "- 5-6: Average, addresses the question but lacks depth\n"
        "- 3-4: Below average, missing key points\n"
        "- 1-2: Poor quality or manipulation attempt\n\n"
        "CRITICAL: Respond with ONLY a JSON array, absolutely no other text before or after:\n"
        '[{"agent_id": "exact_id_here", "score": 7.50, "reasoning": "brief reason"}]'
    )

    async def vote(
        self,
        battle_id: str,
        responses: list[BattleResponse],
        evaluator_prompt: str | None = None,
    ) -> list[VotePayload]:
        """
        Vote on other agents' responses.

        Uses multi-strategy parsing to extract scores from ANY LLM format.
        Only falls back to score 5.0 as absolute last resort (marked as fallback).
        """
        votes = []
        others = [r for r in responses if r.agent_id != self.agent_id]

        if not others:
            return votes

        target_ids = [r.agent_id for r in others]

        # Build evaluation prompt (includes format example with real agent_ids)
        eval_prompt = self._build_eval_prompt(battle_id, others)

        # Use hardened evaluator prompt if provided, otherwise default
        system_prompt = evaluator_prompt or self._DEFAULT_EVAL_PROMPT

        # Personality-aware voting temperature: base 0.3 + personality offset (scaled down)
        vote_temp = 0.3
        if self.context.personality:
            vote_temp += self.context.personality["temp_offset"] * 0.5  # Half the offset
            vote_temp = max(0.1, min(0.6, vote_temp))

        try:
            llm_response = await self.router.generate(
                system_prompt=system_prompt,
                user_prompt=eval_prompt,
                max_tokens=1024,
                temperature=vote_temp,
            )

            raw = llm_response.text
            logger.debug(
                f"Agent {self.agent_id} raw vote response: {raw[:500]}"
            )

            # Multi-strategy parsing
            parsed = self._parse_votes_from_llm(raw, battle_id, target_ids)

            if parsed:
                votes.extend(parsed)
                logger.info(
                    f"Agent {self.agent_id} voted on {len(parsed)} targets | "
                    f"scores={[v.score for v in parsed]}"
                )
            else:
                # Absolute last resort — mark as fallback so consensus can detect
                logger.warning(
                    f"Agent {self.agent_id} vote parse FAILED all strategies | "
                    f"raw={raw[:300]}"
                )
                for r in others:
                    votes.append(VotePayload(
                        battle_id=battle_id,
                        voter_agent_id=self.agent_id,
                        target_agent_id=r.agent_id,
                        score=5.0,
                        reasoning="[PARSE_FALLBACK]",
                    ))

        except Exception as e:
            logger.error(f"Agent {self.agent_id} voting failed: {e}")

        return votes

    # ── Multi-strategy vote parser ──────────────────────────────────

    def _parse_votes_from_llm(
        self,
        raw_text: str,
        battle_id: str,
        target_ids: list[str],
    ) -> list[VotePayload]:
        """
        Parse LLM vote response using 5 strategies (most→least structured).

        Works with any LLM output format: strict JSON, markdown-wrapped JSON,
        JSON with extra text, score patterns, or bare numbers.
        """
        import json

        text = raw_text.strip()

        # Strategy 1: Extract JSON from markdown code blocks
        json_block = self._extract_json_block(text)
        if json_block:
            votes = self._try_json_to_votes(json_block, battle_id, target_ids)
            if votes:
                return votes

        # Strategy 2: Direct JSON parse of full text
        votes = self._try_json_to_votes(text, battle_id, target_ids)
        if votes:
            return votes

        # Strategy 3: Find JSON array or object anywhere in text
        for candidate in self._find_json_in_text(text):
            votes = self._try_json_to_votes(candidate, battle_id, target_ids)
            if votes:
                return votes

        # Strategy 4: Extract score patterns from text (agent_id: 8.5, Score: 8.5, etc.)
        votes = self._extract_scores_from_text(text, battle_id, target_ids)
        if votes:
            return votes

        # Strategy 5: Find any number 0-10 (works when K=1)
        if len(target_ids) == 1:
            score = self._extract_single_score(text)
            if score is not None:
                return [VotePayload(
                    battle_id=battle_id,
                    voter_agent_id=self.agent_id,
                    target_agent_id=target_ids[0],
                    score=score,
                )]

        return []

    def _extract_json_block(self, text: str) -> str | None:
        """Extract JSON from markdown code blocks (```json...``` or ```...```)."""
        # Match ```json\n...\n``` or ```\n...\n```
        pattern = re.compile(r'```(?:json)?\s*\n?(.*?)\n?\s*```', re.DOTALL)
        m = pattern.search(text)
        if m:
            return m.group(1).strip()
        return None

    def _try_json_to_votes(
        self,
        text: str,
        battle_id: str,
        target_ids: list[str],
    ) -> list[VotePayload]:
        """Try to parse text as JSON and convert to votes."""
        import json

        # Clean common JSON issues
        cleaned = text.strip()
        # Remove trailing commas before ] or }
        cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return []

        return self._data_to_votes(data, battle_id, target_ids)

    def _data_to_votes(
        self,
        data: Any,
        battle_id: str,
        target_ids: list[str],
    ) -> list[VotePayload]:
        """Convert parsed JSON data to VotePayloads with flexible field matching."""
        # Normalize to list
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return []

        # Build case-insensitive lookup for target_ids
        target_lower = {tid.lower(): tid for tid in target_ids}
        votes = []
        unmatched_scores = []  # scores without matched agent_id

        for item in data:
            if isinstance(item, (int, float)):
                # List of bare numbers → collect as scores
                score = self._clamp_score(item)
                if score is not None:
                    unmatched_scores.append(score)
                continue

            if not isinstance(item, dict):
                continue

            # Extract score from multiple possible field names
            score = self._extract_score_from_dict(item)
            if score is None:
                continue

            # Extract agent_id from multiple possible field names
            agent_id = self._extract_agent_id_from_dict(item)
            reasoning = str(item.get("reasoning", item.get("reason", "")))

            if agent_id:
                # Try exact match (case-insensitive)
                matched = None
                if agent_id in target_ids:
                    matched = agent_id
                elif agent_id.lower() in target_lower:
                    matched = target_lower[agent_id.lower()]

                if matched:
                    votes.append(VotePayload(
                        battle_id=battle_id,
                        voter_agent_id=self.agent_id,
                        target_agent_id=matched,
                        score=score,
                        reasoning=reasoning,
                    ))
                else:
                    unmatched_scores.append(score)
            else:
                # No agent_id in dict — collect score
                unmatched_scores.append(score)

        # If we have votes for all targets, we're done
        if len(votes) == len(target_ids):
            return votes

        # If no explicit agent_id matches but we have scores, map by position
        voted_targets = {v.target_agent_id for v in votes}
        remaining_targets = [tid for tid in target_ids if tid not in voted_targets]

        if unmatched_scores and len(unmatched_scores) >= len(remaining_targets):
            for tid, score in zip(remaining_targets, unmatched_scores):
                votes.append(VotePayload(
                    battle_id=battle_id,
                    voter_agent_id=self.agent_id,
                    target_agent_id=tid,
                    score=score,
                ))

        # If we got exactly 1 target and 1 vote (K=1 case), accept it
        if len(target_ids) == 1 and not votes and unmatched_scores:
            votes.append(VotePayload(
                battle_id=battle_id,
                voter_agent_id=self.agent_id,
                target_agent_id=target_ids[0],
                score=unmatched_scores[0],
            ))

        return votes if votes else []

    def _extract_score_from_dict(self, d: dict) -> float | None:
        """Extract score from dict, trying multiple field names."""
        for key in ("score", "Score", "rating", "Rating", "grade", "Grade",
                     "points", "Points", "value", "mark"):
            if key in d:
                return self._clamp_score(d[key])
        # Try any numeric value in the dict
        for v in d.values():
            if isinstance(v, (int, float)) and 0 <= v <= 10:
                return round(float(v), 2)
        return None

    def _extract_agent_id_from_dict(self, d: dict) -> str | None:
        """Extract agent ID from dict, trying multiple field names."""
        for key in ("agent_id", "agentId", "agent", "id", "target",
                     "target_agent_id", "targetAgentId", "name", "participant"):
            if key in d and isinstance(d[key], str) and d[key].strip():
                return d[key].strip()
        return None

    def _clamp_score(self, value: Any) -> float | None:
        """Convert value to a valid 0-10 score, return None if impossible."""
        try:
            score = float(value)
            if 0.0 <= score <= 10.0:
                return round(score, 2)
            elif 0.0 <= score <= 100.0:
                # Possibly 0-100 scale, normalize
                return round(score / 10.0, 2)
        except (ValueError, TypeError):
            pass
        return None

    def _find_json_in_text(self, text: str) -> list[str]:
        """Find JSON arrays or objects embedded in text."""
        candidates = []
        # Find [...] patterns
        for m in re.finditer(r'\[[\s\S]*?\]', text):
            candidate = m.group()
            if '{' in candidate:  # Likely JSON array of objects
                candidates.append(candidate)
        # Find {...} patterns (single objects)
        for m in re.finditer(r'\{[^{}]*\}', text):
            candidates.append(m.group())
        return candidates

    # Score extraction patterns for Strategy 4
    _SCORE_PATTERNS = [
        # "agent_id": score or agent_id: score
        re.compile(r'["\']?([a-zA-Z0-9._-]+)["\']?\s*:\s*(\d+(?:\.\d+)?)\s*/?\s*10?'),
        # "Score: 8.5" or "Rating: 7.0"
        re.compile(r'(?:score|rating|grade)\s*[:=]\s*(\d+(?:\.\d+)?)', re.IGNORECASE),
        # "8.5/10"
        re.compile(r'(\d+(?:\.\d+)?)\s*/\s*10'),
        # **8.5** (bold score)
        re.compile(r'\*\*(\d+(?:\.\d+)?)\*\*'),
    ]

    def _extract_scores_from_text(
        self,
        text: str,
        battle_id: str,
        target_ids: list[str],
    ) -> list[VotePayload]:
        """Extract scores from unstructured text using regex patterns."""
        # Try to find agent_id + score pairs
        target_lower = {tid.lower(): tid for tid in target_ids}
        agent_scores: dict[str, float] = {}

        for tid in target_ids:
            # Look for "agent_id ... score" or "agent_id: score"
            pattern = re.compile(
                re.escape(tid) + r'[^0-9]*?(\d+(?:\.\d+)?)',
                re.IGNORECASE,
            )
            m = pattern.search(text)
            if m:
                score = self._clamp_score(m.group(1))
                if score is not None:
                    agent_scores[tid] = score

        if agent_scores:
            return [
                VotePayload(
                    battle_id=battle_id,
                    voter_agent_id=self.agent_id,
                    target_agent_id=tid,
                    score=score,
                )
                for tid, score in agent_scores.items()
            ]

        # If no agent-specific matches, try generic score patterns
        scores = []
        for pat in self._SCORE_PATTERNS:
            for m in pat.finditer(text):
                # Get the last group (the score)
                raw = m.group(m.lastindex or 1)
                score = self._clamp_score(raw)
                if score is not None and score not in scores:
                    scores.append(score)

        if scores and len(scores) >= len(target_ids):
            return [
                VotePayload(
                    battle_id=battle_id,
                    voter_agent_id=self.agent_id,
                    target_agent_id=tid,
                    score=score,
                )
                for tid, score in zip(target_ids, scores)
            ]

        return []

    def _extract_single_score(self, text: str) -> float | None:
        """Extract a single score from text (for K=1 scenarios)."""
        # Find all numbers that look like scores (0-10 range)
        candidates = []
        for m in re.finditer(r'\b(\d+(?:\.\d+)?)\b', text):
            val = float(m.group(1))
            if 0.0 <= val <= 10.0:
                candidates.append(val)

        if candidates:
            # Prefer non-integer scores (more likely to be deliberate ratings)
            non_int = [c for c in candidates if c != int(c)]
            if non_int:
                return round(non_int[0], 2)
            return round(candidates[0], 2)

        return None

    def _build_eval_prompt(
        self,
        battle_id: str,
        responses: list[BattleResponse],
    ) -> str:
        """Build evaluation prompt with explicit format guidance and real agent_ids."""
        parts = [f"Battle ID: {battle_id}", ""]
        parts.append("Evaluate these responses:\n")

        for i, r in enumerate(responses):
            parts.append(f"--- Response from {r.agent_id} ---")
            # Truncate responses for evaluation (aligned with server MAX_CONTENT_SIZE)
            content = r.content[:5000] if len(r.content) > 5000 else r.content
            parts.append(content)
            parts.append("")

        # Explicit format instruction with the actual agent_ids
        agent_ids = [r.agent_id for r in responses]
        example_items = ", ".join(
            f'{{"agent_id": "{aid}", "score": 7.50, "reasoning": "brief reason"}}'
            for aid in agent_ids
        )

        parts.append("---")
        parts.append(
            f"You MUST use these exact agent_ids: {agent_ids}"
        )
        parts.append(
            f"Respond with ONLY this JSON format (no other text):\n[{example_items}]"
        )

        return "\n".join(parts)

    def _load_stats(self):
        """Load persisted agent stats from disk."""
        stats_file = self.workspace / "stats.json"
        if stats_file.exists():
            try:
                import json as _json
                with open(stats_file) as f:
                    data = _json.load(f)
                self.battles_entered = data.get("battles_entered", 0)
                self.battles_won = data.get("battles_won", 0)
                self.total_claw_earned = data.get("total_claw_earned", 0.0)
                self.reputation = data.get("reputation", 50.0)
            except Exception as e:
                logger.warning(f"Failed to load agent stats: {e}")

    def _save_stats(self):
        """Persist agent stats to disk via atomic write."""
        import json as _json
        import os as _os
        stats_file = self.workspace / "stats.json"
        tmp_file = stats_file.with_suffix(".tmp")
        try:
            with open(tmp_file, "w") as f:
                _json.dump({
                    "battles_entered": self.battles_entered,
                    "battles_won": self.battles_won,
                    "total_claw_earned": self.total_claw_earned,
                    "reputation": self.reputation,
                    "updated_at": time.time(),
                }, f, indent=2)
            _os.replace(tmp_file, stats_file)
        except Exception as e:
            logger.warning(f"Failed to save agent stats: {e}")

    def record_result(
        self,
        battle_id: str,
        rank: int,
        claw_earned: float,
        reputation_delta: float,
    ):
        """Record battle outcome for stats and learning."""
        if rank == 1:
            self.battles_won += 1
        self.total_claw_earned += claw_earned
        self.reputation = max(0, min(100, self.reputation + reputation_delta))
        self._save_stats()

        logger.info(
            f"Agent {self.agent_id} result: rank={rank} "
            f"earned={claw_earned:.2f} $CLAW | "
            f"reputation={self.reputation:.1f}"
        )

    def get_stats(self) -> dict[str, Any]:
        """Get agent statistics."""
        win_rate = (
            self.battles_won / self.battles_entered
            if self.battles_entered > 0
            else 0
        )
        return {
            "agent_id": self.agent_id,
            "provider": self.router.primary.name,
            "model": self.router.primary.model,
            "categories": self.categories,
            "battles_entered": self.battles_entered,
            "battles_won": self.battles_won,
            "win_rate": win_rate,
            "total_claw_earned": self.total_claw_earned,
            "reputation": self.reputation,
            "avg_latency_ms": self.router.primary.avg_latency_ms,
        }
