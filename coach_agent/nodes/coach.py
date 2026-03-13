"""
Coach conversation node that generates AI responses using LLM.
"""
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from coach_agent.state import CoachState
from config.prompts.coach_agent_prompt import (
    get_coach_prompt,
    get_coach_greeting,
    get_coach_name
)
from utils.llm import get_llm


async def coach_node(state: CoachState) -> dict:
    """
    Generate a coach response using the LLM.
    
    This node:
    1. Builds the system prompt based on coach type and user config
    2. If first message, generates a personalized greeting
    3. Otherwise, generates a response to the user's message
    
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
    
    # Get user info from config
    user_name = user_config.get("name", "Friend")
    user_gender = user_config.get("gender", "unknown")
    user_age = user_config.get("age", 25)
    
    # Build the system prompt for this coach
    system_prompt = get_coach_prompt(
        coach_type=coach_type,
        user_name=user_name,
        user_gender=user_gender,
        user_age=user_age
    )
    
    # Add session summaries context if available
    if session_summaries:
        summaries_text = "\n".join([f"- {summary}" for summary in session_summaries])
        system_prompt += f"\n\n## Previous Sessions Context\nYou have spoken with this user before. Here are summaries of their past sessions:\n{summaries_text}\n\nUse this context to provide continuity and reference past discussions when relevant."
    
    # Initialize the LLM
    llm = get_llm()
    
    if is_first_message:
        # Generate initial greeting
        greeting = get_coach_greeting(coach_type, user_name)
        
        # Create message chain for generating a personalized intro
        intro_messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content="Please introduce yourself and start the conversation naturally.")
        ]
        
        # Get LLM response for a more dynamic greeting
        response = await llm.ainvoke(intro_messages)
        
        return {
            "messages": [AIMessage(content=response.content)],
            "is_first_message": False
        }
    else:
        # Build message chain with conversation history
        llm_messages = [SystemMessage(content=system_prompt)]
        
        # Add conversation history
        for msg in messages:
            llm_messages.append(msg)
        
        # Generate response
        response = await llm.ainvoke(llm_messages)
        
        return {
            "messages": [AIMessage(content=response.content)]
        }


def get_coach_display_name(coach_type: str) -> str:
    """
    Get the display name for a coach.
    
    Args:
        coach_type: The coach identifier
        
    Returns:
        The coach's display name
    """
    return get_coach_name(coach_type)


