"""
Onboarding node: collect and validate the user's name.

Handles two sub-steps:
  - "init"          → generate greeting + ask for name
  - "awaiting_name" → validate user's response, ask for age on success
"""
import logging
from typing import Optional
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from onboarding_agent.state import OnboardingState
from config.prompts.onboarding_prompt import (
    ONBOARDING_GREETING_PROMPT,
    ONBOARDING_NAME_VALIDATION_PROMPT,
    ONBOARDING_REASK_NAME_PROMPT,
    ONBOARDING_ASK_AGE_PROMPT,
)
from config.settings import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured output schema for name validation
# ---------------------------------------------------------------------------

class NameValidation(BaseModel):
    """Result of validating a user's name response."""
    valid: bool = Field(description="Whether the response contains a usable first name")
    extracted_name: Optional[str] = Field(
        None,
        description="The extracted first name, properly capitalized (e.g. 'Aryan'). Null if invalid.",
    )
    rejection_reason: Optional[str] = Field(
        None,
        description=(
            "If invalid, a short phrase describing why — e.g. 'contains profanity', "
            "'random characters or gibberish', 'is a number or symbol', "
            "'too short or single character', 'user refused or said skip/idk', "
            "'not a recognisable name'. Null if valid."
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


async def _validate_name(user_message: str) -> NameValidation:
    """Use structured LLM output to decide if the message contains a real name."""
    llm = _get_llm(temperature=0).with_structured_output(NameValidation)
    prompt = ONBOARDING_NAME_VALIDATION_PROMPT.format(user_message=user_message)
    result = await llm.ainvoke([SystemMessage(content=prompt)])
    return result


async def _generate_greeting() -> str:
    """Generate a warm first greeting + name ask."""
    llm = _get_llm(temperature=0.8)
    result = await llm.ainvoke([SystemMessage(content=ONBOARDING_GREETING_PROMPT)])
    return result.content.strip()


async def _generate_reask_name(user_message: str, rejection_reason: str) -> str:
    """Generate a gentle re-ask when name validation fails, reflecting the reason."""
    llm = _get_llm(temperature=0.8)
    prompt = ONBOARDING_REASK_NAME_PROMPT.format(
        user_message=user_message,
        rejection_reason=rejection_reason,
    )
    result = await llm.ainvoke([SystemMessage(content=prompt)])
    return result.content.strip()


async def _generate_age_ask(name: str) -> str:
    """Generate a short, natural age question after name is confirmed."""
    llm = _get_llm(temperature=0.8)
    prompt = ONBOARDING_ASK_AGE_PROMPT.format(name=name)
    result = await llm.ainvoke([SystemMessage(content=prompt)])
    return result.content.strip()


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def collect_name_node(state: OnboardingState) -> dict:
    """
    LangGraph node for name collection.

    - step "init": sends greeting + asks for name.
    - step "awaiting_name": validates user_message; on success advances to age ask.
    """
    step = state.get("current_step", "init")
    user_message: Optional[str] = state.get("latest_user_message")

    # --- First contact: no message yet, just greet ---
    if step == "init" or not user_message:
        logger.info(f"Onboarding [{state['user_id']}]: greeting + ask name")
        reply = await _generate_greeting()
        return {
            "current_step": "awaiting_name",
            "reply_text": reply,
            "options_to_send": None,
            "onboarding_complete": False,
        }

    # --- Validate the name they sent ---
    logger.info(f"Onboarding [{state['user_id']}]: validating name '{user_message[:40]}'")
    try:
        validation = await _validate_name(user_message)
    except Exception as e:
        logger.warning(f"Name validation LLM error: {e}. Falling back to re-ask.")
        validation = NameValidation(valid=False, extracted_name=None)

    if validation.valid and validation.extracted_name:
        name = validation.extracted_name
        logger.info(f"Onboarding [{state['user_id']}]: name accepted → '{name}'")
        reply = await _generate_age_ask(name)
        return {
            "collected_name": name,
            "current_step": "awaiting_age",
            "reply_text": reply,
            "options_to_send": None,
            "onboarding_complete": False,
        }
    else:
        reason = validation.rejection_reason or "unclear input"
        logger.info(f"Onboarding [{state['user_id']}]: name invalid ({reason}), re-asking")
        reply = await _generate_reask_name(user_message, rejection_reason=reason)
        return {
            "current_step": "awaiting_name",
            "reply_text": reply,
            "options_to_send": None,
            "onboarding_complete": False,
        }
