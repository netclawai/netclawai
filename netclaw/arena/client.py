"""
Arena Client — Connect your agent to a remote NetClaw Arena.

The client polls the Arena REST API for active Battles,
submits responses, and participates in consensus voting.

Battle lifecycle (from the client's perspective):
  1. Poll /api/status for active battles
  2. Fetch challenge prompt and generate a response via LLM
  3. Submit response to /api/battles/submit
  4. When phase switches to "voting", fetch assigned targets
  5. Evaluate responses and submit votes
  6. Earn $CLAW rewards based on consensus scores

Usage:
    netclaw agent join --arena https://arena.netclaw.io
    netclaw agent join --arena http://localhost:8421
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.parse as _urlparse
from typing import Optional

import httpx

from netclaw.core.agent import NetClawAgent, BattleTask, BattleResponse

logger = logging.getLogger("netclaw.client")

MAX_RESPONSE_SIZE = 1_048_576  # 1 MB

_HTTP_TIMEOUT = httpx.Timeout(
    connect=10.0,
    read=120.0,
    write=30.0,
    pool=10.0,
)

_DEFAULT_HEADERS = {
    "User-Agent": "NetClaw-Agent/1.0",
}


def _safe_error_text(text: str, max_len: int = 200) -> str:
    """Redact potential credentials from server error responses before logging."""
    text = text[:max_len]
    text = re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", text)
    text = re.sub(r"[a-fA-F0-9]{32,}", "[REDACTED]", text)
    text = re.sub(r"(?:sk|gsk|xai)[_-][a-zA-Z0-9_-]{16,}", "[REDACTED]", text)
    return text


class ArenaClient:
    """
    Connects a local agent to a remote Arena server.

    Polls the Arena REST API for active Battles, submits responses
    during the competition phase, then evaluates and votes during
    the voting phase. Uses exponential backoff on connection failures.
    """

    def __init__(
        self,
        agent: NetClawAgent,
        arena_url: str = "https://arena.netclaw.io",
        poll_interval: int = 10,
        wallet: str = "",
        arena_key: str = "",
        agent_secret: str = "",
    ):
        self.agent = agent
        self.arena_url = arena_url.rstrip("/")
        self.poll_interval = poll_interval
        self._wallet = wallet
        self._arena_key = arena_key
        self._agent_secret = agent_secret
        self._running = False
        self._last_battle_id: Optional[str] = None
        self._submitted_battle_id: Optional[str] = None
        self._voted_battle_id: Optional[str] = None
        self._failed_battle_id: Optional[str] = None
        self._registered = False
        self._session_token: Optional[str] = None
        self._backoff_seconds = 0       # Current backoff delay (0 = no backoff)
        self._backoff_max = 300         # Cap at 5 minutes
        self._backoff_base = 5          # Start at 5 seconds
        self._consecutive_successes = 0
        self._backoff_reset_threshold = 3

        # Warn about insecure HTTP for non-localhost connections
        _parsed = _urlparse.urlparse(self.arena_url)
        _is_local = _parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if _parsed.scheme != "https" and not _is_local:
            if self._arena_key or self._agent_secret:
                logger.warning(
                    "WARNING: Sending credentials (arena_key/agent_secret) over insecure HTTP! "
                    "Use HTTPS for production arenas."
                )
            else:
                logger.warning(f"Connecting to arena over insecure HTTP: {arena_url}")

        logger.info(
            f"ArenaClient initialized | "
            f"agent={agent.agent_id} | "
            f"arena={arena_url} | "
            f"auth={'yes' if arena_key else 'no'}"
        )

    @staticmethod
    def _check_response_size(r: httpx.Response, context: str = "") -> bool:
        """Check server response size. Returns True if within limit."""
        size = len(r.content)
        if size > MAX_RESPONSE_SIZE:
            logger.warning(
                f"Oversized response from server ({size} bytes, max {MAX_RESPONSE_SIZE}) "
                f"— {context or r.url}. Ignoring response."
            )
            return False
        return True

    def _auth_headers(self) -> dict[str, str]:
        """Build Authorization headers if arena_key is configured."""
        headers = {}
        if self._arena_key:
            headers["Authorization"] = f"Bearer {self._arena_key}"
        elif self._session_token:
            headers["X-Session-Token"] = self._session_token
        if self._agent_secret:
            headers["X-Agent-Secret"] = self._agent_secret
        return headers

    async def start(self):
        """Connect to arena and start competing."""
        self._running = True

        # Register with arena
        await self._register()

        logger.info(
            f"🏟️  Agent '{self.agent.agent_id}' connected to {self.arena_url}"
        )
        logger.info(f"⚔️  Listening for Battles (polling every {self.poll_interval}s)...")

        while self._running:
            try:
                await self._poll_and_compete()
                if self._backoff_seconds > 0:
                    self._consecutive_successes += 1
                    if self._consecutive_successes >= self._backoff_reset_threshold:
                        logger.info("Connection stable — backoff fully reset")
                        self._backoff_seconds = 0
                        self._consecutive_successes = 0
                    else:
                        # Halve the delay on each success (gradual cooldown)
                        self._backoff_seconds = max(
                            self._backoff_base,
                            self._backoff_seconds // 2,
                        )
                        logger.info(
                            f"Connection ok ({self._consecutive_successes}/"
                            f"{self._backoff_reset_threshold} for full reset) — "
                            f"backoff reduced to {self._backoff_seconds}s"
                        )
            except httpx.ConnectError:
                self._consecutive_successes = 0
                self._backoff_seconds = min(
                    self._backoff_seconds * 2 or self._backoff_base,
                    self._backoff_max,
                )
                logger.warning(
                    f"Cannot reach arena at {self.arena_url}. "
                    f"Retrying in {self._backoff_seconds}s..."
                )
                await asyncio.sleep(self._backoff_seconds)
                continue
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 403 and "session token" in e.response.text.lower():
                    logger.warning("Session token rejected — re-registering with arena...")
                    self._session_token = None
                    await self._register()
                elif e.response.status_code in (502, 503, 504):
                    logger.warning(
                        f"Arena temporarily unavailable ({e.response.status_code}). "
                        f"Retrying in {self.poll_interval}s..."
                    )
                else:
                    logger.error(f"HTTP error: {e.response.status_code} {_safe_error_text(e.response.text)}")
            except Exception as e:
                logger.error(f"Client error: {type(e).__name__}: {e}")

            await asyncio.sleep(self.poll_interval)

    async def stop(self):
        self._running = False
        logger.info(f"Agent '{self.agent.agent_id}' disconnected")

    async def _register(self):
        """Register this agent with the remote arena."""
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=_DEFAULT_HEADERS) as client:
                payload = {
                    "agent_id": self.agent.agent_id,
                    "provider": self.agent.router.primary.name,
                    "model": self.agent.router.primary.model,
                    "categories": self.agent.categories,
                }
                if self._wallet:
                    payload["wallet"] = self._wallet
                if self._agent_secret:
                    payload["agent_secret"] = self._agent_secret
                r = await client.post(
                    f"{self.arena_url}/api/agents/register",
                    json=payload,
                    headers=self._auth_headers(),
                )
                if r.status_code == 200:
                    if not self._check_response_size(r, "register"):
                        return
                    self._registered = True
                    resp_data = r.json()
                    if "session_token" in resp_data:
                        token = str(resp_data["session_token"])
                        if len(token) <= 256:
                            self._session_token = token
                            logger.debug("Session token received from arena")
                        else:
                            logger.warning("Ignoring oversized session_token from server")
                    logger.info(f"✅ Registered with arena")
                elif r.status_code == 403 and "entry requires" in r.text.lower():
                    # Cross-arena entry score threshold — agent doesn't meet minimum
                    try:
                        detail = r.json().get("detail", r.text)
                    except Exception:
                        detail = r.text
                    logger.error(f"⛔ Registration rejected — {detail}")
                    self._running = False
                else:
                    logger.warning(f"Registration response: {r.status_code}")
        except Exception as e:
            logger.warning(f"Could not register (arena may not support it yet): {e}")

    async def _poll_and_compete(self):
        """Poll for active battle — compete and vote."""
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=_DEFAULT_HEADERS) as client:
            # Auto-retry registration if previous attempt failed
            if not self._registered:
                await self._register()

            # Check arena status
            r = await client.get(f"{self.arena_url}/api/status", headers=self._auth_headers())
            r.raise_for_status()
            if not self._check_response_size(r, "status"):
                return
            status = r.json()

            active = status.get("active_battle")
            if not active:
                return

            battle_id = active.get("battle_id", "")
            phase = active.get("phase", "")
            category = active.get("category", "")

            # Voting phase: we submitted a response, now vote on others
            if phase == "voting" and self._submitted_battle_id == battle_id and self._voted_battle_id != battle_id:
                await self._participate_in_voting(battle_id, client)
                self._voted_battle_id = battle_id
                return

            # Skip if already competed or failed in this battle
            if battle_id == self._submitted_battle_id:
                return
            if battle_id == self._failed_battle_id:
                return

            # Only compete during competing phase
            if phase != "competing":
                return

            # Check category match
            if category not in self.agent.categories:
                logger.debug(f"Skipping battle {battle_id}: category '{category}' not in my list")
                return

            logger.info(
                f"Battle detected! id={battle_id} | "
                f"category={category} | phase={phase}"
            )

            # Use prompt/bounty/salt from status response (included since server v2)
            # Falls back to /api/battles/active only if prompt is missing from status
            MAX_PROMPT_SIZE = 10_000
            prompt = active.get("prompt", "")
            if len(prompt) > MAX_PROMPT_SIZE:
                logger.warning(f"Oversized prompt ({len(prompt)} chars) from server, truncating to {MAX_PROMPT_SIZE}")
                prompt = prompt[:MAX_PROMPT_SIZE]
            try:
                bounty = float(active.get("bounty", 200))
                import math as _math_bounty
                if not (0 <= bounty <= 1_000_000) or _math_bounty.isnan(bounty) or _math_bounty.isinf(bounty):
                    bounty = 200
            except (ValueError, TypeError):
                bounty = 200
            prompt_salt = active.get("prompt_salt", "")

            if not prompt:
                # Fallback: fetch from dedicated endpoint (older servers)
                try:
                    r = await client.get(f"{self.arena_url}/api/battles/active", headers=self._auth_headers())
                    r.raise_for_status()
                    if not self._check_response_size(r, "battles/active"):
                        return
                    battle_data = r.json()
                    prompt = battle_data.get("prompt", "")
                    bounty = battle_data.get("bounty", 100)
                    prompt_salt = battle_data.get("prompt_salt", "")
                except httpx.HTTPStatusError as e:
                    logger.warning(
                        f"Cannot fetch battle details: {e.response.status_code} "
                        f"{_safe_error_text(e.response.text, 300)}. Skipping."
                    )
                    return
                except Exception as e:
                    logger.warning(f"Cannot fetch battle details: {e}. Skipping.")
                    return

            if not prompt:
                logger.warning("No prompt available for battle. Skipping.")
                return

            # Log selection info if available
            spots = active.get("spots_remaining")
            if spots is not None:
                logger.info(
                    f"Slots disponibili: {spots}/{active.get('max_agents', '?')}"
                )

            # Create BattleTask
            task = BattleTask(
                battle_id=battle_id,
                category=category,
                prompt=prompt,
                bounty_claw=bounty,
                prompt_salt=prompt_salt,
            )

            # Compete
            try:
                response = await self.agent.compete(task)

                # Submit response to arena
                r = await client.post(
                    f"{self.arena_url}/api/battles/submit",
                    json={
                        "battle_id": battle_id,
                        "agent_id": self.agent.agent_id,
                        "content": response.content,
                        "latency_ms": response.latency_ms,
                        "provider": response.provider_used,
                        "model": response.model_used,
                    },
                    headers=self._auth_headers(),
                )

                if r.status_code == 200:
                    logger.info(
                        f"Response submitted | "
                        f"latency={response.latency_ms:.0f}ms | "
                        f"provider={response.provider_used}"
                    )
                    self._submitted_battle_id = battle_id
                elif r.status_code == 409:
                    # Battle full — cap reached
                    try:
                        err = r.json()
                    except Exception:
                        err = {}
                    eligible = err.get("eligible_count", "?")
                    max_agents = err.get("max_agents", 1000)
                    logger.warning(
                        f"Battle piena! {max_agents}/{eligible} slot occupati. "
                        f"Riprova al prossimo round."
                    )
                    # Mark as submitted to avoid retrying this battle
                    self._submitted_battle_id = battle_id
                elif r.status_code == 403 and "session token" in r.text.lower():
                    logger.warning("Session token rejected on submit — re-registering...")
                    self._session_token = None
                    await self._register()
                else:
                    logger.warning(f"Submit failed: {r.status_code} {_safe_error_text(r.text)}")

            except Exception as e:
                logger.error(f"Competition failed: {e}")
                self._failed_battle_id = battle_id

    async def _participate_in_voting(self, battle_id: str, client: httpx.AsyncClient):
        """Fetch assigned responses and submit votes during the voting phase."""
        try:
            # Server returns only our assigned voting targets (K random responses)
            r = await client.get(
                f"{self.arena_url}/api/battles/active/responses",
                params={"agent_id": self.agent.agent_id},
                headers=self._auth_headers(),
            )
            if r.status_code != 200:
                logger.warning(f"Cannot fetch responses for voting: {r.status_code}")
                return

            if not self._check_response_size(r, "active/responses"):
                return

            data = r.json()
            responses_data = data.get("responses", [])

            # Convert to BattleResponse objects with size limits
            MAX_VOTING_CONTENT = 50_000  # 50 KB per response
            responses = []
            for rd in responses_data[:50]:
                try:
                    aid = str(rd.get("agent_id", "unknown"))[:64]
                    content = str(rd.get("content", ""))
                    if len(content) > MAX_VOTING_CONTENT:
                        logger.warning(
                            f"Oversized voting response ({len(content)} chars) from server, truncating"
                        )
                        content = content[:MAX_VOTING_CONTENT]
                    responses.append(BattleResponse(
                        battle_id=battle_id,
                        agent_id=aid,
                        content=content,
                        latency_ms=rd.get("latency_ms", 0),
                        provider_used=str(rd.get("provider", "unknown"))[:64],
                        model_used=str(rd.get("model", "unknown"))[:64],
                    ))
                except Exception:
                    continue

            # Generate votes
            votes = await self.agent.vote(battle_id, responses)

            # Submit votes
            for vote in votes:
                try:
                    r = await client.post(
                        f"{self.arena_url}/api/battles/vote",
                        json={
                            "battle_id": vote.battle_id,
                            "voter_agent_id": vote.voter_agent_id,
                            "target_agent_id": vote.target_agent_id,
                            "score": vote.score,
                            "reasoning": vote.reasoning,
                        },
                        headers=self._auth_headers(),
                    )
                    if r.status_code != 200:
                        logger.warning(f"Vote rejected: {r.status_code}")
                except Exception as e:
                    logger.warning(f"Vote submit failed: {type(e).__name__}")

            logger.info(f"Submitted {len(votes)} votes for battle {battle_id[:8]}")

        except Exception as e:
            logger.error(f"Voting failed: {e}")
