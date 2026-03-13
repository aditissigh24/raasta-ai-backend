"""
LangGraph agent for the relationship coach chatbot.
"""
from langgraph.graph import StateGraph, START, END
from langchain_core.messages import HumanMessage

from coach_agent.state import CoachState
from coach_agent.nodes.fetch_configuration import fetch_configuration_node
from coach_agent.nodes.coach import coach_node
import logging 

logger = logging.getLogger(__name__)
def should_fetch_config(state: CoachState) -> str:
    """
    Determine if we need to fetch configuration.
    
    Args:
        state: Current conversation state
        
    Returns:
        Next node name: "fetch_config" or "coach"
    """
    if not state.get("config_fetched", False):
        return "fetch_config"
    return "coach"


def create_coach_agent():
    """
    Create and compile the coach agent graph.
    
    The graph flow:
    1. START -> Check if config needed
    2. If first time: fetch_config -> coach -> END
    3. If config exists: coach -> END
    
    Returns:
        Compiled LangGraph agent
    """
    # Create the state graph
    graph = StateGraph(CoachState)
    
    # Add nodes
    graph.add_node("fetch_config", fetch_configuration_node)
    graph.add_node("coach", coach_node)
    
    # Add conditional edge from START
    graph.add_conditional_edges(
        START,
        should_fetch_config,
        {
            "fetch_config": "fetch_config",
            "coach": "coach"
        }
    )
    
    # After fetching config, go to coach
    graph.add_edge("fetch_config", "coach")
    
    # Coach node ends the graph
    graph.add_edge("coach", END)
    
    # Compile and return
    return graph.compile()


async def run_coach_agent(
    session_id: str,
    user_id: str,
    coach_type: str,
    user_message: str | None = None,
    existing_messages: list | None = None,
    user_config: dict | None = None,
    config_fetched: bool = False,
    session_summaries: list | None = None
) -> dict:
    """
    Run the coach agent for a conversation turn.
    
    Args:
        session_id: Unique session identifier
        user_id: User's unique identifier
        coach_type: The coach persona to use
        user_message: The user's message (None for initial greeting)
        existing_messages: Previous messages in the conversation
        user_config: Pre-fetched user configuration
        config_fetched: Whether config has already been fetched
        session_summaries: List of past session summaries for context
        
    Returns:
        Dict containing the agent's response and updated state
    """
    agent = create_coach_agent()
    
    # Build initial state
    messages = existing_messages or []
    
    # Add user message if provided
    if user_message:
        messages.append(HumanMessage(content=user_message))

    
    
    initial_state: CoachState = {
        "messages": messages,
        "user_config": user_config or {"user_id": user_id},
        "coach_type": coach_type,
        "session_id": session_id,
        "is_first_message": len(messages) == 0,
        "config_fetched": config_fetched,
        "session_summaries": session_summaries or []
    }

    logger.info(f"info going in llm : {initial_state}")
    
    # Run the agent
    result = await agent.ainvoke(initial_state)
    
    # Extract the last AI message as the response
    ai_messages = [
        msg for msg in result["messages"]
        if hasattr(msg, "type") and msg.type == "ai"
    ]
    
    response_content = ""
    if ai_messages:
        response_content = ai_messages[-1].content
    
    return {
        "response": response_content,
        "messages": result["messages"],
        "user_config": result["user_config"],
        "config_fetched": result["config_fetched"]
    }


