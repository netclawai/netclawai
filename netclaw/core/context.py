"""
Context Builder — Constructs optimized prompts for Battle responses.

Assembles a system prompt from category-specific instructions, strategy
directives, and past battle memory. The challenge prompt is truncated
to 10K chars to match the arena's content size limit.

Personality System (4 layers):
  Layer 1 — Personality (semi-stable): agent_id + session_entropy → approach + tone.
    Changes every restart (session_entropy is random), so an attacker reading
    the code CANNOT predict which personality an agent will have.

  Layer 2 — Category overlay: category-specific approach modifiers that
    strengthen the base personality for the current challenge type.

  Layer 3 — Battle seed (per-battle): agent_id + battle_id + prompt_salt +
    session_entropy → unique angle + micro temperature jitter. The prompt_salt
    is generated server-side with secrets.token_hex(), and session_entropy is
    private to each client process, making the angle fully unpredictable.

  Layer 4 — Strategy modifier: per-agent strategic emphasis selected from
    agent_id + session_entropy. Ensures [Strategy] is unique even when all
    agents use the same default config.

Combinatorial space: 24 approach × 16 tone × 14 overlay × 30 angle × 12 strategy
= 1,935,360 per category × session_entropy = effectively infinite.
"""

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass, field

logger = logging.getLogger("netclaw.context")

VALID_CATEGORIES = {"text", "code", "reasoning", "creative", "knowledge"}
_BATTLE_ID_RE = re.compile(r'^[a-fA-F0-9-]{8,64}\Z')


@dataclass
class Context:
    system_prompt: str = ""
    user_prompt: str = ""
    max_tokens: int = 2048
    temperature: float = 0.7
    extra_params: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


# Category-specific system prompts for Battle optimization
CATEGORY_PROMPTS = {
    "text": (
        "You excel at text analysis, rewriting, summarization, and translation. "
        "Be clear, accurate, and well-structured."
    ),
    "code": (
        "You are an expert programmer. Write clean, efficient, well-commented code. "
        "Include error handling. Explain your approach briefly."
    ),
    "reasoning": (
        "You are a logical reasoning expert. Think step by step. "
        "Show your work clearly. Verify your conclusions."
    ),
    "creative": (
        "You are a creative writing expert. Be original, engaging, and vivid. "
        "Use strong imagery and compelling narrative structure."
    ),
    "knowledge": (
        "You are a knowledgeable expert. Provide accurate, well-sourced information. "
        "Distinguish between established facts and uncertain claims."
    ),
}


# ── Layer 1: Personality Fingerprint (24 approach × 16 tone) ─────
# Semi-stable: same within a session, different across restarts.
# Uses session_entropy (random at startup) so code-reading attackers
# cannot predict which personality an agent will have.

APPROACH_STYLES = [
    "You MUST use concrete examples and practical applications to illustrate every point.",
    "You MUST use analogies and metaphors — translate abstract concepts into vivid comparisons.",
    "You MUST be systematic: number every step, show your process explicitly.",
    "You MUST go deep on the single most important point rather than covering everything superficially.",
    "You MUST start with your conclusion/answer, THEN explain the reasoning that led to it.",
    "You MUST write as if explaining to a smart colleague over coffee — conversational but rigorous.",
    "You MUST focus on edge cases, limitations, and what could go wrong.",
    "You MUST compare at least 2 different approaches before recommending one.",
    "You MUST optimize for clarity and simplicity — if a simpler explanation exists, use it.",
    "You MUST be exhaustive — cover all angles, leave no question unanswered.",
    "You MUST reason from first principles, building up from the most basic truths.",
    "You MUST highlight what is surprising or counterintuitive about this topic.",
    "You MUST structure your response as a narrative with a clear beginning, middle, and end.",
    "You MUST focus on actionable takeaways — what should someone DO with this information?",
    "You MUST use a Socratic approach: pose the key questions, then answer them one by one.",
    "You MUST take a strong, definitive position and defend it with evidence.",
    "You MUST organize your answer using clear sections with headers or bullet points.",
    "You MUST use specific numbers, data, or quantitative reasoning wherever possible.",
    "You MUST consider the problem from multiple perspectives before synthesizing an answer.",
    "You MUST explain your confidence level and what would change your answer.",
    # ── v2 additions ──
    "You MUST teach through a single detailed worked example from start to finish.",
    "You MUST present the information as a trade-off analysis: pros, cons, and recommendation.",
    "You MUST use the 'what, so what, now what' framework: state the fact, explain significance, suggest action.",
    "You MUST layer your answer: give the 10-second summary first, then the full detailed analysis.",
]

RESPONSE_TONES = [
    "professional and precise",
    "enthusiastic and engaging",
    "calm and analytical",
    "direct and no-nonsense",
    "thoughtful and nuanced",
    "confident and authoritative",
    "curious and exploratory",
    "concise and efficient",
    "witty and sharp",
    "measured and balanced",
    # ── v2 additions ──
    "warm and approachable",
    "incisive and provocative",
    "methodical and thorough",
    "crisp and decisive",
    "reflective and contemplative",
    "pragmatic and results-oriented",
]


# ── Layer 2: Category-Specific Overlays (14 per category) ────────
# Each category has its own set of approach modifiers. Selected per
# agent+category (deterministic within session, varies across agents).

CATEGORY_OVERLAYS = {
    "code": [
        "Optimize for readability and maintainability over cleverness.",
        "Include edge case handling and explain why each guard clause exists.",
        "Show the simplest working solution first, then discuss optimizations.",
        "Add inline comments explaining the non-obvious WHY, not the obvious WHAT.",
        "Structure your code so that the most important logic is visible first.",
        "Consider time/space complexity and mention the Big-O explicitly.",
        "Write code that a junior developer could understand on first reading.",
        "Use idiomatic patterns for the language — avoid reinventing standard solutions.",
        # ── v2 additions ──
        "Test your solution mentally with at least one edge case before presenting it.",
        "Prefer standard library solutions over custom implementations when available.",
        "Make the code's intent clear from function and variable names alone.",
        "Show how you would handle the most likely failure mode gracefully.",
        "If there's a common gotcha in this problem, highlight it explicitly.",
        "Structure your code so the happy path reads top-to-bottom without deep nesting.",
    ],
    "reasoning": [
        "Explicitly state each assumption before building on it.",
        "Use proof by contradiction or elimination to strengthen your argument.",
        "Identify the weakest link in your reasoning and address it directly.",
        "Quantify uncertainty — say 'likely' vs 'certain' and explain why.",
        "Check your answer by working backwards from the conclusion.",
        "Separate what you KNOW from what you INFER — be explicit about both.",
        "If there are multiple valid interpretations, address each one.",
        "Use a structured format: Given → Therefore → Because.",
        # ── v2 additions ──
        "Explicitly enumerate all possible cases before analyzing each one.",
        "Challenge your own conclusion with the strongest counterargument.",
        "If a shortcut exists, show both the shortcut and why it works.",
        "Distinguish between necessary and sufficient conditions explicitly.",
        "Scale the problem: verify your logic holds for extreme inputs.",
        "Build your argument as a chain where each link is independently verifiable.",
    ],
    "creative": [
        "Take a creative risk that other responses probably won't — be bold.",
        "Use a unique structural device: non-linear timeline, unusual POV, frame story.",
        "Ground abstract themes in specific, sensory details.",
        "Create tension or surprise — subvert the reader's expectation at least once.",
        "Use dialogue or internal monologue to reveal character, not exposition.",
        "Find the emotional core of the prompt and build outward from it.",
        "Avoid clichés — if your first idea feels obvious, push past it.",
        "End with an image or line that resonates after reading.",
        # ── v2 additions ──
        "Choose a vivid setting and ground the reader there in the first two sentences.",
        "Use rhythm and pacing deliberately — slow for emotional beats, fast for action.",
        "Give your characters a specific desire that drives every action.",
        "Use a structural constraint: repeated motif, mirrored opening and closing, or symmetry.",
        "Layer at least two meanings or interpretations into your piece.",
        "Write the ending first in your mind, then build toward it with foreshadowing.",
    ],
    "text": [
        "Prioritize the information hierarchy — most important facts first.",
        "Use transitions that show logical relationships between ideas.",
        "Vary sentence length — short for impact, longer for nuance.",
        "Cut every word that doesn't earn its place in the response.",
        "Use specific details instead of vague generalizations.",
        "Make your structure visible — the reader should always know where they are.",
        "Anticipate the reader's follow-up question and address it proactively.",
        "Use parallel structure for related ideas to aid comprehension.",
        # ── v2 additions ──
        "Open with the single most surprising or important finding.",
        "Use concrete numbers and specifics instead of vague qualifiers like 'many' or 'often'.",
        "End each paragraph with a sentence that connects to the next topic.",
        "Write for someone who will only read the first and last sentences of each paragraph.",
        "Distinguish clearly between what is proven and what is your interpretation.",
        "Use the inverted pyramid: most important information first, supporting details after.",
    ],
    "knowledge": [
        "Distinguish between consensus knowledge and areas of active debate.",
        "Cite the level of evidence: anecdotal, observational, experimental, meta-analysis.",
        "Identify common misconceptions about this topic and correct them.",
        "Explain what the current limitations of our knowledge are.",
        "Use the most recent data available — note when information might be outdated.",
        "Connect specialized knowledge to practical implications.",
        "Present competing theories fairly before stating which is better supported.",
        "Quantify where possible — 'doubles the risk' is better than 'increases the risk'.",
        # ── v2 additions ──
        "Start with what is definitively known, then build toward areas of uncertainty.",
        "Use analogies to connect unfamiliar concepts to everyday experience.",
        "Highlight the practical implications of each key fact you mention.",
        "Note where the scientific consensus has changed recently and explain why.",
        "Provide the historical context that makes the current understanding make sense.",
        "Identify the single most important takeaway and make it unmissable.",
    ],
}


# ── Layer 3: Per-Battle Seed (30 angles) ─────────────────────────
# Dynamic: changes every battle. Uses server-provided prompt_salt PLUS
# client-private session_entropy, making the angle fully unpredictable
# even if an attacker knows agent_id, battle_id, and prompt_salt.

BATTLE_ANGLES = [
    "Lead with your strongest argument first, then support it.",
    "Consider the problem from an unconventional or contrarian angle.",
    "Provide a concrete worked example before generalizing.",
    "Start by identifying the core constraint or limitation.",
    "Frame your entire answer around a real-world use case or scenario.",
    "Challenge the most obvious assumption before answering.",
    "Build your answer incrementally, from the simplest case to the complex one.",
    "Anticipate the most likely objection and address it upfront.",
    "Identify the hidden trade-off that most answers would miss.",
    "Connect this to a broader principle or universal pattern.",
    "Focus on what would change your answer if one key detail were different.",
    "Explain this as if the reader has never encountered the topic before.",
    "Structure your answer as a decision tree: if X then Y, otherwise Z.",
    "Start with what most people get wrong about this topic.",
    "Give the shortest possible correct answer, then expand only where needed.",
    "Approach this like a detective: gather evidence, then reach a conclusion.",
    "Focus on the WHY behind the answer, not just the WHAT.",
    "Use a before/after or problem/solution framing.",
    "Find the simplest mental model that captures the essential truth.",
    "Break this into exactly 3 key insights, no more no less.",
    # ── v2 additions ──
    "Start with the answer, then show your work — don't make the reader wait.",
    "Find the pattern: connect this to another domain where the same principle applies.",
    "Identify the constraint everyone else will ignore and address it head-on.",
    "Apply the Pareto principle: find the 20% of information that provides 80% of the value.",
    "Reason by analogy: find the closest well-understood parallel and map the differences.",
    "Start by defining the key terms precisely — many wrong answers come from ambiguity.",
    "Zoom out first: what category of problem is this, and what general strategies apply?",
    "Present the strongest version of the argument you disagree with, then refute it.",
    "Focus on the transition points: where does the simple case break down?",
    "Build your answer around a single memorable insight that ties everything together.",
]


# ── Layer 4: Strategy Modifiers (12 options) ─────────────────────
# Ensures [Strategy] is unique per agent even when all agents share
# the same default config. Selected per agent_id + session_entropy.

STRATEGY_MODIFIERS = [
    "Prioritize depth over breadth in your response.",
    "Lead with the most practical and actionable information.",
    "Emphasize precision and correctness above all else.",
    "Aim for elegance — the best answer is often the simplest one.",
    "Focus on what makes this problem interesting or unique.",
    "Prioritize completeness — address every aspect of the question.",
    "Write for maximum impact with minimum words.",
    "Balance theory with practice — show both the why and the how.",
    "Optimize your answer for someone who will act on it immediately.",
    "Ensure your response stands alone — no external knowledge needed.",
    "Prioritize originality — avoid the most obvious or common answer.",
    "Structure your response for both quick scanning and deep reading.",
]


# Temperature offset range
_TEMP_JITTER_RANGE = 0.20  # ±0.20 around category base


def personality_fingerprint(agent_id: str, session_entropy: str = "") -> dict:
    """Generate a personality from agent_id + session_entropy.

    With session_entropy (random at startup), the personality changes every
    restart — making it unpredictable from the code alone.

    Returns:
        {"approach": str, "tone": str, "temp_offset": float}
    """
    material = f"{agent_id}:{session_entropy}" if session_entropy else agent_id
    h = hashlib.sha256(material.encode()).hexdigest()
    approach_idx = int(h[:8], 16) % len(APPROACH_STYLES)
    tone_idx = int(h[8:16], 16) % len(RESPONSE_TONES)
    temp_raw = int(h[16:24], 16) / 0xFFFFFFFF
    temp_offset = (temp_raw * 2 - 1) * _TEMP_JITTER_RANGE

    return {
        "approach": APPROACH_STYLES[approach_idx],
        "tone": RESPONSE_TONES[tone_idx],
        "temp_offset": round(temp_offset, 3),
    }


def category_overlay(agent_id: str, category: str, session_entropy: str = "") -> str:
    """Select a category-specific approach modifier for this agent.

    Returns an empty string if category has no overlays.
    """
    overlays = CATEGORY_OVERLAYS.get(category)
    if not overlays:
        return ""
    material = f"{agent_id}:{category}:{session_entropy}"
    h = hashlib.sha256(material.encode()).hexdigest()
    idx = int(h[:8], 16) % len(overlays)
    return overlays[idx]


def battle_seed(
    agent_id: str,
    battle_id: str,
    prompt_salt: str = "",
    session_entropy: str = "",
) -> dict:
    """Generate a per-battle angle from agent_id + battle_id + server salt + session entropy.

    The prompt_salt is generated server-side with secrets.token_hex(),
    and session_entropy is private to each client process. Together they
    make the angle fully unpredictable — even knowing agent_id, battle_id,
    and the public prompt_salt is NOT enough.

    Returns:
        {"angle": str, "temp_micro": float}
    """
    combined = f"{agent_id}:{battle_id}:{prompt_salt}:{session_entropy}"
    h = hashlib.sha256(combined.encode()).hexdigest()
    angle_idx = int(h[:8], 16) % len(BATTLE_ANGLES)
    temp_raw = int(h[8:16], 16) / 0xFFFFFFFF
    temp_micro = (temp_raw * 2 - 1) * 0.05

    return {
        "angle": BATTLE_ANGLES[angle_idx],
        "temp_micro": round(temp_micro, 3),
    }


def strategy_modifier(agent_id: str, session_entropy: str = "") -> str:
    """Select a strategy emphasis modifier for this agent.

    Ensures [Strategy] is unique per agent even when all agents share
    the same default config. Uses a different hash slice than personality
    to avoid correlation.

    Returns a strategy modifier string.
    """
    material = f"strat:{agent_id}:{session_entropy}"
    h = hashlib.sha256(material.encode()).hexdigest()
    idx = int(h[:8], 16) % len(STRATEGY_MODIFIERS)
    return STRATEGY_MODIFIERS[idx]


class ContextBuilder:
    """Builds optimized prompts for Battle responses."""

    CHARS_PER_TOKEN = 4

    def __init__(self, memory, strategy, max_input_tokens: int = 4096,
                 agent_id: str = ""):
        self.memory = memory
        self.strategy = strategy
        self.max_input_tokens = max_input_tokens
        self._agent_id = agent_id
        # Session entropy: random at startup, makes personality non-deterministic
        self._session_entropy = secrets.token_hex(8) if agent_id else ""
        self.personality = (
            personality_fingerprint(agent_id, self._session_entropy)
            if agent_id else None
        )

    def build_for_battle(
        self,
        prompt: str,
        category: str,
        battle_id: str,
        prompt_salt: str = "",
    ) -> Context:
        """Build context optimized for a specific Battle category.

        Args:
            prompt: The challenge text.
            category: Battle category (text/code/reasoning/creative/knowledge).
            battle_id: UUID of the current battle.
            prompt_salt: Server-provided random salt for this battle (optional).
        """
        if not isinstance(prompt, str):
            raise TypeError(f"Prompt must be a string, got {type(prompt).__name__}")
        MAX_PROMPT_LEN = 10_000
        if not prompt or not prompt.strip():
            raise ValueError("Empty prompt")
        if len(prompt) < 3:
            raise ValueError(f"Prompt too short ({len(prompt)} chars, minimum 3)")
        prompt = prompt[:MAX_PROMPT_LEN]

        if not isinstance(category, str):
            raise TypeError(f"Category must be a string, got {type(category).__name__}")
        if category not in VALID_CATEGORIES:
            logger.warning(
                f"Unknown category '{category}', falling back to 'text'. "
                f"Valid: {sorted(VALID_CATEGORIES)}"
            )
            category = "text"

        if not isinstance(battle_id, str):
            raise TypeError(f"battle_id must be a string, got {type(battle_id).__name__}")
        if not battle_id or not battle_id.strip():
            raise ValueError("Empty battle_id")
        if not _BATTLE_ID_RE.match(battle_id):
            raise ValueError(
                f"Invalid battle_id format: '{battle_id[:32]}' "
                f"(must be 8-64 hex/UUID chars)"
            )

        # Category-specific system prompt
        cat_prompt = CATEGORY_PROMPTS.get(category, CATEGORY_PROMPTS["text"])

        # Strategy directives
        strategy_dir = self.strategy.get_directives()

        # Memory hints from past battles
        memory_hints = self.memory.get_relevant_context(
            challenge_type=category,
            limit=3,
        )

        # Assemble system prompt
        system_parts = [cat_prompt]

        # Layer 1: Personality fingerprint (semi-stable, per-session)
        if self.personality:
            system_parts.append(
                f"[Approach] {self.personality['approach']} "
                f"Your tone should be {self.personality['tone']}."
            )

        # Layer 2: Category-specific overlay
        if self._agent_id:
            cat_over = category_overlay(
                self._agent_id, category, self._session_entropy
            )
            if cat_over:
                system_parts.append(f"[Category] {cat_over}")

        # Layer 3: Per-battle seed (dynamic, server-salted + private session entropy)
        bseed = None
        if self._agent_id:
            bseed = battle_seed(
                self._agent_id, battle_id, prompt_salt,
                session_entropy=self._session_entropy,
            )
            system_parts.append(f"[Focus] {bseed['angle']}")

        # Layer 4: Strategy modifier (per-agent, diversifies default strategy)
        if self._agent_id:
            strat_mod = strategy_modifier(self._agent_id, self._session_entropy)
            if strategy_dir:
                system_parts.append(f"[Strategy] {strategy_dir} {strat_mod}")
            else:
                system_parts.append(f"[Strategy] {strat_mod}")
        elif strategy_dir:
            system_parts.append(f"[Strategy] {strategy_dir}")

        if memory_hints:
            system_parts.append(
                f"[Memory] Past winning patterns: {memory_hints}"
            )

        system_parts.append(
            "Respond directly. Quality and accuracy determine your score. "
            "Your response will be evaluated by other AI agents."
        )

        system_prompt = "\n".join(system_parts)

        # Budget check
        budget = self.max_input_tokens * self.CHARS_PER_TOKEN
        if len(system_prompt) + len(prompt) > budget:
            available = budget - len(prompt) - 100
            system_prompt = system_prompt[:max(200, available)] + "..."

        # Temperature: category base + personality offset + battle micro-jitter
        temperature = self._get_temperature_for_category(category)
        if self.personality:
            temperature += self.personality["temp_offset"]
        if bseed:
            temperature += bseed["temp_micro"]
        temperature = max(0.1, min(1.0, temperature))

        return Context(
            system_prompt=system_prompt,
            user_prompt=prompt,
            max_tokens=self.strategy.get_max_tokens(),
            temperature=temperature,
            extra_params=self.strategy.get_creative_params(),
            metadata={
                "category": category,
                "battle_id": battle_id,
                "memory_used": bool(memory_hints),
            },
        )

    def _get_temperature_for_category(self, category: str) -> float:
        """Adjust temperature per category for optimal results."""
        base = self.strategy.get_temperature()
        adjustments = {
            "text": 0.0,         # Use base
            "code": -0.2,        # Lower for precision
            "reasoning": -0.3,   # Lowest for logic
            "creative": +0.2,    # Higher for creativity
            "knowledge": -0.1,   # Slightly lower for accuracy
        }
        adjusted = base + adjustments.get(category, 0.0)
        return max(0.1, min(1.0, adjusted))
