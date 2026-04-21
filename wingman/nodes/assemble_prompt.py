"""
assemble_prompt node — builds system_prompt from Langfuse templates + DB data.

Stable section (guardrails + character + scene) is Redis-cached per character+scenario.
Dynamic section (current beat) is freshly built each turn.
"""
import asyncio
import logging

from wingman.state.wingman_state import ConversationState
from wingman.prompts import langfuse_loader
from wingman.db.ctx_cache import get_stable_prompt, set_stable_prompt

logger = logging.getLogger(__name__)


async def assemble_prompt(state: ConversationState) -> dict:
    """
    Return stable_section, dynamic_section, and system_prompt.

    Stable section is cached in Redis keyed by character_id + scenario_id.
    """
    character = state["character"]
    scenario  = state["scenario"]
    beat      = state["current_beat"]
    user_profile = state.get("user_profile") or {}

    char_id = character.get("id", "")
    scen_id = scenario.get("id", "")

    # ── Stable section (Redis-cached per character + scenario) ──────────────
    stable_section = await get_stable_prompt(char_id, scen_id)

    if not stable_section:
        logger.info(
            f"assemble_prompt: stable section cache miss — fetching 3 Langfuse templates "
            f"| char={char_id} scenario={scen_id}"
        )
        guardrails_tpl, character_tpl, scene_tpl = await asyncio.gather(
            langfuse_loader.get("wingman/base/guardrails"),
            langfuse_loader.get("wingman/base/character-persona"),
            langfuse_loader.get("wingman/base/scene-directive"),
        )
        logger.info(
            f"assemble_prompt: Langfuse templates fetched "
            f"(guardrails, character-persona, scene-directive) | char={char_id}"
        )

        guardrails = guardrails_tpl.compile({
            "character_name": character.get("name", ""),
            "hard_limits":    ", ".join(character.get("hard_limits", [])),
        })
        character_section = character_tpl.compile({
            "character_name":    character.get("name", ""),
            "age":               str(character.get("age", "")),
            "gender":            character.get("gender", ""),
            "city":              character.get("city", ""),
            "archetype":         character.get("archetype", ""),
            "personality_traits": character.get("vibe_summary", ""),
            "backstory":         character.get("backstory", ""),
            "speaking_style":    character.get("speaking_style", ""),
            "emoji_usage":       character.get("emoji_usage", ""),
            "texting_speed":     character.get("texting_speed", ""),
            "voice_prompt":      character.get("voice_prompt", ""),
        })
        scene_section = scene_tpl.compile({
            "scenario_title":      scenario.get("scenario_title", ""),
            "setting_description": scenario.get("setting_description", ""),
            "atmosphere":          scenario.get("atmosphere", ""),
            "tone":                scenario.get("tone", ""),
            "time_of_day":         scenario.get("time_of_day", ""),
            "overall_arc":         scenario.get("overall_arc", ""),
        })

        stable_section = "\n\n---\n\n".join([guardrails, character_section, scene_section])

        # Cache for subsequent turns
        await set_stable_prompt(char_id, scen_id, stable_section)
        logger.info(
            f"assemble_prompt: stable section built and cached | "
            f"char={char_id} len={len(stable_section)}"
        )
    else:
        logger.info(
            f"assemble_prompt: stable section from Redis cache | "
            f"char={char_id} scenario={scen_id} len={len(stable_section)}"
        )

    # ── Dynamic section (fresh each turn) ───────────────────────────────────
    inject_hook = state.get("inject_hook", False)
    directive = beat.get("hook_directive", "") if inject_hook else beat.get("flow_directive", "")

    if inject_hook:
        logger.info(f"assemble_prompt: inject_hook=True — fetching engagement-injection template")
        injection_tpl = await langfuse_loader.get("wingman/base/engagement-injection")
        injection = injection_tpl.compile({"character_name": character.get("name", "")})
        directive = f"{directive}\n\n{injection}"

    logger.info(
        f"assemble_prompt: fetching current-beat template | "
        f"beat={beat.get('beat_number', '?')} beat_type={beat.get('beat_type', '?')}"
    )
    beat_tpl = await langfuse_loader.get("wingman/base/current-beat")
    dynamic_section = beat_tpl.compile({
        "narrative_context":         beat.get("narrative_context", ""),
        "character_emotional_state": beat.get("character_emotional_state", ""),
        "directive":                 directive,
    })

    FINAL_CONSTRAINTS = """---
    Final Output Constraints (HIGHEST PRIORITY — OVERRIDES EVERYTHING ABOVE):
    - Max 3 sentences
    - Max 150 characters
    - No narration, no actions, no stage directions
    - Only what the character would text
    If conflict exists with any previous instruction → follow THIS section."""

    user_context = _build_user_context(user_profile)

    system_prompt = f"{stable_section}\n\n---\n\n{user_context}\n\n---\n\n{dynamic_section}\n\n---\n\n{FINAL_CONSTRAINTS}"
    logger.info(
        f"assemble_prompt: system prompt assembled | "
        f"stable_len={len(stable_section)} dynamic_len={len(dynamic_section)} "
        f"total_len={len(system_prompt)}"
    )

    return {
        "stable_section":  stable_section,
        "dynamic_section": dynamic_section,
        "system_prompt":   system_prompt,
    }


def _build_user_context(user_profile: dict) -> str:
    """Build a short user-context paragraph from the user's profile fields."""
    name      = user_profile.get("name", "").strip()
    age_range = user_profile.get("age_range", "").strip()
    gender    = user_profile.get("gender", "").strip()

    if not any([name, age_range, gender]):
        return "User Context:\nYou are chatting with a user. Adapt your tone accordingly."

    parts = []
    if name:
        parts.append(f"Their name is {name}")
    if age_range:
        parts.append(f"they are {age_range} years old")
    if gender:
        parts.append(f"they identify as {gender}")

    detail = ", ".join(parts) + "."
    logger.info(f"User context: {detail}")
    return f"User Context:\nYou are talking to a real person. {detail} Address them by name when it feels natural and calibrate your energy, flirtiness, and emotional depth to match someone of this age and gender."
