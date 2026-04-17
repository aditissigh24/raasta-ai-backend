"""
Onboarding node: handle chapter selection.

Called when current_step == "awaiting_chapter".
- If a user message is present: try to match it to a chapter.
  - Match found → fetch scenarios for that chapter and present them.
  - No match → re-present chapter chips with a gentle nudge.
- If no user message (e.g. reconnect): re-present chapter chips.
"""
import logging
from typing import Optional
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage

from onboarding_agent.state import OnboardingState
from onboarding_agent.scenario_fields import character_id_from_scenario
from config.prompts.onboarding_prompt import (
    ONBOARDING_CHAPTER_NOT_FOUND_PROMPT,
    ONBOARDING_PRESENT_SCENARIOS_PROMPT,
)
from config.settings import settings

logger = logging.getLogger(__name__)


def _get_llm(temperature: float = 0.8) -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.OPENAI_MODEL,
        temperature=temperature,
        api_key=settings.OPENAI_API_KEY,
    )


def _match_chapter(chapters: list, user_message: str) -> Optional[dict]:
    """
    Try to match the user's message to a chapter.

    Matching order:
    1. Exact ID match (chip sends the id directly)
    2. Case-insensitive name match
    3. Partial name match (user typed part of the name)
    """
    if not user_message or not chapters:
        return None

    msg = user_message.strip().lower()

    for ch in chapters:
        if str(ch.get("id", "")).lower() == msg:
            return ch

    for ch in chapters:
        if ch.get("name", "").lower() == msg:
            return ch

    for ch in chapters:
        name_lower = ch.get("name", "").lower()
        if msg in name_lower or name_lower in msg:
            return ch

    return None


def _format_chapter_chips(chapters: list) -> list:
    chips = []
    for ch in chapters:
        chip = {"id": str(ch.get("id", "")), "label": ch.get("name", "")}
        subtext = ch.get("description") or ch.get("scenarioCount")
        if subtext:
            chip["subtext"] = (
                f"{subtext} scenarios" if isinstance(subtext, int) else str(subtext)
            )
        chips.append(chip)
    return chips


def _format_scenario_chips(scenarios: list) -> list:
    chips = []
    for sc in scenarios:
        chip = {
            "id": str(sc.get("scenario_id", sc.get("id", ""))),
            "label": sc.get("scenario_title", sc.get("title", "")),
            "characterId": character_id_from_scenario(sc),
        }
        subtext = sc.get("situation_setup_for_user") or sc.get("difficulty")
        if subtext:
            chip["subtext"] = str(subtext)
        chips.append(chip)
    return chips


async def _generate_not_found_message(user_message: str) -> str:
    llm = _get_llm()
    prompt = ONBOARDING_CHAPTER_NOT_FOUND_PROMPT.format(user_message=user_message)
    result = await llm.ainvoke([SystemMessage(content=prompt)])
    return result.content.strip()


async def _generate_scenario_intro(chapter_name: str) -> str:
    llm = _get_llm()
    prompt = ONBOARDING_PRESENT_SCENARIOS_PROMPT.format(chapter_name=chapter_name)
    result = await llm.ainvoke([SystemMessage(content=prompt)])
    return result.content.strip()


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def present_chapters_node(state: OnboardingState) -> dict:
    """
    LangGraph node for chapter selection.

    Matches the user's message to a chapter.
    On match: fetches scenarios and shows them as chips.
    On no match / no message: re-presents chapter chips.
    """
    user_message: Optional[str] = state.get("latest_user_message")
    chapters = state.get("chapters") or []

    # Ensure we have chapters to work with
    if not chapters:
        try:
            from services.backend_client import backend_client
            chapters = await backend_client.fetch_chapters()
        except Exception as e:
            logger.warning(f"Failed to fetch chapters: {e}")
            chapters = []

    # No message → re-present chips (reconnect case)
    if not user_message:
        chips = _format_chapter_chips(chapters)
        return {
            "chapters": chapters,
            "current_step": "awaiting_chapter",
            "reply_text": "Tap the situation you want to work on 👇",
            "options_to_send": chips if chips else None,
            "onboarding_complete": False,
        }

    # Try to match chapter
    matched = _match_chapter(chapters, user_message)

    if not matched:
        logger.info(
            f"Onboarding [{state['user_id']}]: chapter not matched for '{user_message[:40]}'"
        )
        reply = await _generate_not_found_message(user_message)
        chips = _format_chapter_chips(chapters)
        return {
            "chapters": chapters,
            "current_step": "awaiting_chapter",
            "reply_text": reply,
            "options_to_send": chips if chips else None,
            "onboarding_complete": False,
        }

    chapter_id = str(matched.get("id", ""))
    chapter_name = matched.get("name", "")
    logger.info(f"Onboarding [{state['user_id']}]: chapter selected → '{chapter_name}'")

    # Fetch scenarios for selected chapter
    scenarios = []
    try:
        from services.backend_client import backend_client
        scenarios = await backend_client.fetch_scenarios_by_chapter(chapter_id)
    except Exception as e:
        logger.warning(f"Failed to fetch scenarios for chapter {chapter_id}: {e}")

    reply = await _generate_scenario_intro(chapter_name)
    chips = _format_scenario_chips(scenarios)

    return {
        "chapters": chapters,
        "selected_chapter_id": chapter_id,
        "selected_chapter_name": chapter_name,
        "scenarios": scenarios,
        "current_step": "awaiting_scenario",
        "reply_text": reply,
        "options_to_send": chips if chips else None,
        "onboarding_complete": False,
    }
