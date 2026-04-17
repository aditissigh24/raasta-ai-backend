"""
Roleplay node: generates character responses and optional Wingman feedback using LLM.
"""
import logging
from typing import AsyncGenerator
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from roleplay_agent.state import RoleplayState
from config.prompts.roleplay_prompt import (
    get_character_prompt,
    get_character_opening,
    get_wingman_feedback_messages,
    character_initiates,
)
from utils.llm import get_llm

logger = logging.getLogger(__name__)


async def roleplay_node(state: RoleplayState) -> dict:
    """
    Generate a character response (and optional Wingman tip) for the current turn.

    Behaviour:
    - First message + character initiates scenario → emit the character's opening line
      without calling the LLM (deterministic, fast).
    - First message + user initiates scenario → LLM responds to the user's first message
      in character.
    - Subsequent messages → LLM continues the conversation in character.

    The Wingman feedback is generated as a second, lightweight LLM call and returned
    in the ``wingman_tip`` state key so the socket worker can include it in the response.

    Args:
        state: Current conversation state.

    Returns:
        Dict with updated ``messages`` and optional ``wingman_tip``.
    """
    character_data = state.get("character_data", {})
    scenario_data = state.get("scenario_data", {})
    user_config = state.get("user_config", {})
    is_first_message = state.get("is_first_message", True)
    messages = state.get("messages", [])
    scenario_id = state.get("scenario_id", "")
    session_summaries = state.get("session_summaries", [])

    user_name = user_config.get("name", "Friend")

    # ------------------------------------------------------------------
    # First message + character initiates: return hardcoded/generated opening
    # ------------------------------------------------------------------
    if is_first_message and character_initiates(scenario_id):
        opening = get_character_opening(scenario_id, character_data)
        logger.info(
            f"Character {character_data.get('name')} initiates scenario {scenario_id}: {opening[:60]}..."
        )
        return {
            "messages": [AIMessage(content=opening)],
            "is_first_message": False,
            "wingman_tip": None,
        }

    # ------------------------------------------------------------------
    # Build system prompt
    # ------------------------------------------------------------------
    system_prompt = get_character_prompt(
        char_data=character_data,
        scenario_data=scenario_data,
        user_name=user_name,
    )

    # Append past session summaries for continuity
    if session_summaries:
        summaries_text = "\n".join(f"- {s}" for s in session_summaries)
        system_prompt += (
            "\n\n## Context from previous sessions\n"
            f"{summaries_text}\n"
            "Reference past conversations naturally if relevant."
        )

    llm = get_llm()

    # ------------------------------------------------------------------
    # First message + user initiates: LLM responds to their opening
    # ------------------------------------------------------------------
    if is_first_message:
        llm_messages = [SystemMessage(content=system_prompt)] + list(messages)
        response = await llm.ainvoke(llm_messages)
        character_reply = response.content

        wingman_tip = await _generate_wingman_tip(
            llm=llm,
            scenario_data=scenario_data,
            user_message=_last_user_message(messages),
            character_reply=character_reply,
            char_data=character_data,
        )

        return {
            "messages": [AIMessage(content=character_reply)],
            "is_first_message": False,
            "wingman_tip": wingman_tip,
        }

    # ------------------------------------------------------------------
    # Ongoing conversation
    # ------------------------------------------------------------------
    llm_messages = [SystemMessage(content=system_prompt)] + list(messages)
    response = await llm.ainvoke(llm_messages)
    character_reply = response.content

    wingman_tip = await _generate_wingman_tip(
        llm=llm,
        scenario_data=scenario_data,
        user_message=_last_user_message(messages),
        character_reply=character_reply,
        char_data=character_data,
    )

    return {
        "messages": [AIMessage(content=character_reply)],
        "wingman_tip": wingman_tip,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _last_user_message(messages: list) -> str:
    """Extract the content of the most recent HumanMessage."""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage) or (
            hasattr(msg, "type") and msg.type == "human"
        ):
            return msg.content
    return ""


async def _generate_wingman_tip(
    llm,
    scenario_data: dict,
    user_message: str,
    character_reply: str,
    char_data: dict,
) -> str | None:
    """
    Run the Wingman feedback LLM call.

    Returns the tip string, or None if inputs are missing or the call fails.
    """
    if not user_message or not character_reply:
        return None

    try:
        wingman_messages = get_wingman_feedback_messages(
            scenario_data=scenario_data,
            user_message=user_message,
            character_reply=character_reply,
            char_data=char_data,
        )
        tip_response = await llm.ainvoke(wingman_messages)
        return tip_response.content
    except Exception as e:
        logger.warning(f"Wingman feedback generation failed: {e}")
        return None


async def stream_character_reply(
    character_data: dict,
    scenario_data: dict,
    user_config: dict,
    messages: list,
    scenario_id: str,
    session_summaries: list,
) -> AsyncGenerator[str, None]:
    """
    Async generator that streams the character's reply token-by-token.

    Yields raw content strings as they arrive from the LLM.

    Args:
        character_data: Character record from the backend.
        scenario_data: Scenario record from the backend.
        user_config: User profile dict.
        messages: Full conversation history (LangChain message objects).
        scenario_id: Scenario identifier used for prompt selection.
        session_summaries: Past session summary strings for continuity.

    Yields:
        Content string chunks from the LLM stream.
    """
    user_name = user_config.get("name", "Friend")

    system_prompt = get_character_prompt(
        char_data=character_data,
        scenario_data=scenario_data,
        user_name=user_name,
    )

    if session_summaries:
        summaries_text = "\n".join(f"- {s}" for s in session_summaries)
        system_prompt += (
            "\n\n## Context from previous sessions\n"
            f"{summaries_text}\n"
            "Reference past conversations naturally if relevant."
        )

    llm = get_llm()
    llm_messages = [SystemMessage(content=system_prompt)] + list(messages)

    async for chunk in llm.astream(llm_messages):
        content = chunk.content
        if content:
            yield content
