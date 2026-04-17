"""
Onboarding node: collect and validate the user's age.

On success: fetches chapters from backend and presents them as chips.
On failure: re-asks naturally.
"""
import logging
from typing import Optional
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage

from onboarding_agent.state import OnboardingState
from config.prompts.onboarding_prompt import (
    ONBOARDING_AGE_VALIDATION_PROMPT,
    ONBOARDING_REASK_AGE_PROMPT,
    ONBOARDING_REASK_AGE_OUT_OF_RANGE_PROMPT,
    ONBOARDING_WELCOME_BEFORE_AGE_PROMPT,
    ONBOARDING_PRESENT_CHAPTERS_PROMPT,
)
from config.settings import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured output schema
# ---------------------------------------------------------------------------

class AgeValidation(BaseModel):
    """Result of validating a user's age response."""
    valid: bool = Field(description="Whether a valid age (16-55) was found in the response")
    extracted_age: Optional[int] = Field(
        None,
        description="The numeric age extracted from the response. Null if no number found.",
    )
    rejection_reason: Optional[str] = Field(
        None,
        description=(
            "If invalid AND no number could be extracted, a short phrase describing why — "
            "e.g. 'user refused or said idk', 'vague answer with no number', "
            "'non-numeric gibberish'. Null if a number was extracted (even if out of range)."
        ),
    )


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _get_llm(temperature: float = 0.0) -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.OPENAI_MODEL,
        temperature=temperature,
        api_key=settings.OPENAI_API_KEY,
    )


async def _validate_age(user_message: str) -> AgeValidation:
    llm = _get_llm(temperature=0).with_structured_output(AgeValidation)
    prompt = ONBOARDING_AGE_VALIDATION_PROMPT.format(user_message=user_message)
    result = await llm.ainvoke([SystemMessage(content=prompt)])
    return result


async def _generate_welcome_and_age_ask(name: str) -> str:
    llm = _get_llm(temperature=0.8)
    prompt = ONBOARDING_WELCOME_BEFORE_AGE_PROMPT.format(name=name)
    result = await llm.ainvoke([SystemMessage(content=prompt)])
    return result.content.strip()


async def _generate_reask_age(
    user_message: str,
    extracted_age: Optional[int] = None,
    rejection_reason: Optional[str] = None,
) -> str:
    llm = _get_llm(temperature=0.8)
    if extracted_age is not None:
        # A number was found but is outside the 16–55 range — explain the range
        prompt = ONBOARDING_REASK_AGE_OUT_OF_RANGE_PROMPT.format(extracted_age=extracted_age)
    else:
        # No number found at all — reflect the specific reason why
        prompt = ONBOARDING_REASK_AGE_PROMPT.format(
            user_message=user_message,
            rejection_reason=rejection_reason or "unclear input",
        )
    result = await llm.ainvoke([SystemMessage(content=prompt)])
    return result.content.strip()


async def _generate_chapter_intro(name: str, age: int) -> str:
    llm = _get_llm(temperature=0.8)
    prompt = ONBOARDING_PRESENT_CHAPTERS_PROMPT.format(name=name, age=age)
    result = await llm.ainvoke([SystemMessage(content=prompt)])
    return result.content.strip()


def _format_chapter_chips(chapters: list) -> list:
    """Convert backend chapter records to UI chip format."""
    chips = []
    for ch in chapters:
        chip = {
            "id": str(ch.get("id", "")),
            "label": ch.get("name", ""),
        }
        subtext = ch.get("description") or ch.get("scenarioCount")
        if subtext:
            chip["subtext"] = (
                f"{subtext} scenarios"
                if isinstance(subtext, int)
                else str(subtext)
            )
        chips.append(chip)
    return chips


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def collect_age_node(state: OnboardingState) -> dict:
    """
    LangGraph node for age collection.

    Validates the user's message as an age.
    On success: fetches chapters and presents them as chips.
    On failure: re-asks naturally.
    """
    user_message: Optional[str] = state.get("latest_user_message")
    name: str = state.get("collected_name") or "there"

    if not user_message:
        # First contact on reconnect — send a proper Wingman welcome + age ask
        reply = await _generate_welcome_and_age_ask(name)
        return {
            "current_step": "awaiting_age",
            "reply_text": reply,
            "options_to_send": None,
            "onboarding_complete": False,
        }

    logger.info(f"Onboarding [{state['user_id']}]: validating age '{user_message[:40]}'")
    try:
        validation = await _validate_age(user_message)
    except Exception as e:
        logger.warning(f"Age validation LLM error: {e}. Falling back to re-ask.")
        validation = AgeValidation(valid=False, extracted_age=None)

    if not validation.valid or not validation.extracted_age:
        if validation.extracted_age is not None:
            reason = "out of range"
        else:
            reason = validation.rejection_reason or "unclear input"
        logger.info(f"Onboarding [{state['user_id']}]: age invalid ({reason}), re-asking")
        reply = await _generate_reask_age(
            user_message,
            extracted_age=validation.extracted_age,
            rejection_reason=validation.rejection_reason,
        )
        return {
            "current_step": "awaiting_age",
            "reply_text": reply,
            "options_to_send": None,
            "onboarding_complete": False,
        }

    age = validation.extracted_age
    logger.info(f"Onboarding [{state['user_id']}]: age accepted → {age}")

    # Fetch chapters (may be cached in state already)
    chapters = state.get("chapters") or []
    if not chapters:
        try:
            from services.backend_client import backend_client
            chapters = await backend_client.fetch_chapters()
        except Exception as e:
            logger.warning(f"Failed to fetch chapters: {e}. Using empty list.")
            chapters = []

    reply = await _generate_chapter_intro(name, age)
    chips = _format_chapter_chips(chapters)

    return {
        "collected_age": age,
        "chapters": chapters,
        "current_step": "awaiting_chapter",
        "reply_text": reply,
        "options_to_send": chips if chips else None,
        "onboarding_complete": False,
    }
