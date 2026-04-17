"""
Coach conversation node — legacy, kept for reference only.
This node is not used in the current roleplay flow.
"""
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from config.prompts.coach_agent_prompt import (
    get_coach_prompt,
    get_coach_greeting,
    get_coach_name
)
from utils.llm import get_llm


async def coach_node(state: dict) -> dict:
    """
    Generate a coach response using the LLM.

    Args:
        state: The current conversation state

    Returns:
        Updated state with the coach's response message
    """
    coach_type = state["coach_type"]
    user_config = state["user_config"]
    is_first_message = state.get("is_first_message", True)
    messages = state.get("messages", [])
    session_summaries = state.get("session_summaries", [])

    user_name = user_config.get("name", "Friend")
    user_gender = user_config.get("gender", "unknown")
    user_age = user_config.get("age", 25)

    system_prompt = get_coach_prompt(
        coach_type=coach_type,
        user_name=user_name,
        user_gender=user_gender,
        user_age=user_age
    )

    if session_summaries:
        summaries_text = "\n".join([f"- {summary}" for summary in session_summaries])
        system_prompt += (
            f"\n\n## Previous Sessions Context\nYou have spoken with this user before. "
            f"Here are summaries of their past sessions:\n{summaries_text}\n\n"
            "Use this context to provide continuity and reference past discussions when relevant."
        )

    llm = get_llm()

    if is_first_message:
        intro_messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content="Please introduce yourself and start the conversation naturally.")
        ]
        response = await llm.ainvoke(intro_messages)
        return {
            "messages": [AIMessage(content=response.content)],
            "is_first_message": False
        }
    else:
        llm_messages = [SystemMessage(content=system_prompt)] + list(messages)
        response = await llm.ainvoke(llm_messages)
        return {
            "messages": [AIMessage(content=response.content)]
        }


def get_coach_display_name(coach_type: str) -> str:
    """Get the display name for a coach."""
    return get_coach_name(coach_type)
