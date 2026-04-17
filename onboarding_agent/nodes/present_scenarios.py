"""
Onboarding node: handle scenario selection.

Called when current_step == "awaiting_scenario".
- If user message matches a scenario → advance to complete node logic.
- If no match / no message → re-present scenario chips.
"""
import logging
from typing import Optional
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage

from onboarding_agent.state import OnboardingState
from onboarding_agent.scenario_fields import character_id_from_scenario
from config.prompts.onboarding_prompt import (
    ONBOARDING_SCENARIO_NOT_FOUND_PROMPT,
    ONBOARDING_COMPLETE_PROMPT,
)
from config.settings import settings

logger = logging.getLogger(__name__)


def _get_llm(temperature: float = 0.8) -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.OPENAI_MODEL,
        temperature=temperature,
        api_key=settings.OPENAI_API_KEY,
    )


def _match_scenario(scenarios: list, user_message: str) -> Optional[dict]:
    """
    Match the user's message to a scenario.

    Matching order:
    1. Exact scenario_id / id match (chip sends id directly)
    2. Case-insensitive title match
    3. Partial title match
    """
    if not user_message or not scenarios:
        return None

    msg = user_message.strip().lower()

    for sc in scenarios:
        sc_id = str(sc.get("scenario_id", sc.get("id", ""))).lower()
        if sc_id == msg:
            return sc

    for sc in scenarios:
        title = sc.get("scenario_title", sc.get("title", "")).lower()
        if title == msg:
            return sc

    for sc in scenarios:
        title = sc.get("scenario_title", sc.get("title", "")).lower()
        if msg in title or title in msg:
            return sc

    return None


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
    prompt = ONBOARDING_SCENARIO_NOT_FOUND_PROMPT.format(user_message=user_message)
    result = await llm.ainvoke([SystemMessage(content=prompt)])
    return result.content.strip()


async def _generate_complete_message(scenario_title: str) -> str:
    llm = _get_llm()
    prompt = ONBOARDING_COMPLETE_PROMPT.format(scenario_title=scenario_title)
    result = await llm.ainvoke([SystemMessage(content=prompt)])
    return result.content.strip()


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def present_scenarios_node(state: OnboardingState) -> dict:
    """
    LangGraph node for scenario selection.

    Matches the user's message to a scenario.
    On match: marks onboarding complete.
    On no match / no message: re-presents scenario chips.
    """
    user_message: Optional[str] = state.get("latest_user_message")
    scenarios = state.get("scenarios") or []

    # Ensure scenarios are populated
    if not scenarios:
        chapter_id = state.get("selected_chapter_id")
        if chapter_id:
            try:
                from services.backend_client import backend_client
                scenarios = await backend_client.fetch_scenarios_by_chapter(chapter_id)
            except Exception as e:
                logger.warning(f"Failed to re-fetch scenarios: {e}")

    # No message → re-present chips
    if not user_message:
        chips = _format_scenario_chips(scenarios)
        return {
            "scenarios": scenarios,
            "current_step": "awaiting_scenario",
            "reply_text": "Pick the scenario you'd like to practice 👇",
            "options_to_send": chips if chips else None,
            "onboarding_complete": False,
        }

    # Try to match scenario
    matched = _match_scenario(scenarios, user_message)

    if not matched:
        logger.info(
            f"Onboarding [{state['user_id']}]: scenario not matched for '{user_message[:40]}'"
        )
        reply = await _generate_not_found_message(user_message)
        chips = _format_scenario_chips(scenarios)
        return {
            "scenarios": scenarios,
            "current_step": "awaiting_scenario",
            "reply_text": reply,
            "options_to_send": chips if chips else None,
            "onboarding_complete": False,
        }

    scenario_id = str(matched.get("scenario_id", matched.get("id", "")))
    character_id = character_id_from_scenario(matched)
    scenario_title = matched.get("scenario_title", matched.get("title", "this scenario"))

    logger.info(
        f"Onboarding [{state['user_id']}]: scenario selected → "
        f"'{scenario_title}' (char={character_id})"
    )

    reply = await _generate_complete_message(scenario_title)

    return {
        "scenarios": scenarios,
        "selected_scenario_id": scenario_id,
        "selected_character_id": character_id,
        "current_step": "complete",
        "reply_text": reply,
        "options_to_send": None,
        "onboarding_complete": True,
    }
