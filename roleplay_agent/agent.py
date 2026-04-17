"""
LangGraph agent for the roleplay chatbot.
"""
import logging
from langgraph.graph import StateGraph, START, END
from langchain_core.messages import HumanMessage

from roleplay_agent.state import RoleplayState
from roleplay_agent.nodes.fetch_configuration import fetch_configuration_node
from roleplay_agent.nodes.roleplay import roleplay_node

logger = logging.getLogger(__name__)


def should_fetch_config(state: RoleplayState) -> str:
    """
    Route to fetch_config if config hasn't been loaded yet, otherwise go straight to roleplay.
    """
    if not state.get("config_fetched", False):
        return "fetch_config"
    return "roleplay"


def create_roleplay_agent():
    """
    Build and compile the roleplay LangGraph agent.

    Graph flow:
        START → should_fetch_config
                  ├─ fetch_config → roleplay → END
                  └─ roleplay → END
    """
    graph = StateGraph(RoleplayState)

    graph.add_node("fetch_config", fetch_configuration_node)
    graph.add_node("roleplay", roleplay_node)

    graph.add_conditional_edges(
        START,
        should_fetch_config,
        {
            "fetch_config": "fetch_config",
            "roleplay": "roleplay",
        },
    )

    graph.add_edge("fetch_config", "roleplay")
    graph.add_edge("roleplay", END)

    return graph.compile()


async def run_roleplay_agent(
    session_id: str,
    user_id: str,
    character_id: str,
    scenario_id: str,
    user_message: str | None = None,
    existing_messages: list | None = None,
    user_config: dict | None = None,
    character_data: dict | None = None,
    scenario_data: dict | None = None,
    config_fetched: bool = False,
    session_summaries: list | None = None,
) -> dict:
    """
    Run one turn of the roleplay agent.

    Args:
        session_id: Unique session identifier.
        user_id: User's unique identifier.
        character_id: Character identifier (e.g. "C01").
        scenario_id: Scenario identifier (e.g. "S01").
        user_message: The user's latest message (None on the very first turn
                      when the character initiates).
        existing_messages: Prior conversation messages in LangChain format.
        user_config: Pre-fetched user config dict (skips backend fetch if provided).
        character_data: Pre-fetched character dict (skips backend fetch if provided).
        scenario_data: Pre-fetched scenario dict (skips backend fetch if provided).
        config_fetched: Set True when all config dicts are already supplied.
        session_summaries: Past session summary strings for continuity context.

    Returns:
        Dict with keys:
            response      – character's reply text
            wingman_tip   – Wingman feedback string (may be None)
            messages      – full updated message list
            user_config   – user config (possibly freshly fetched)
            character_data
            scenario_data
            config_fetched
    """
    agent = create_roleplay_agent()

    messages = list(existing_messages or [])
    if user_message:
        messages.append(HumanMessage(content=user_message))

    initial_state: RoleplayState = {
        "messages": messages,
        "user_config": user_config or {"user_id": user_id},
        "character_data": character_data or {},
        "scenario_data": scenario_data or {},
        "character_id": character_id,
        "scenario_id": scenario_id,
        "session_id": session_id,
        "is_first_message": len(messages) == 0 or (
            len(messages) == 1 and user_message is not None
        ),
        "config_fetched": config_fetched,
        "session_summaries": list(session_summaries or []),
    }

    logger.info(
        f"Running roleplay agent | char={character_id} scene={scenario_id} "
        f"session={session_id[:20]}... msgs={len(messages)}"
    )

    result = await agent.ainvoke(initial_state)

    # Extract the last AI message as the character's reply
    ai_messages = [
        msg for msg in result["messages"]
        if hasattr(msg, "type") and msg.type == "ai"
    ]
    response_content = ai_messages[-1].content if ai_messages else ""

    return {
        "response": response_content,
        "wingman_tip": result.get("wingman_tip"),
        "messages": result["messages"],
        "user_config": result["user_config"],
        "character_data": result.get("character_data", {}),
        "scenario_data": result.get("scenario_data", {}),
        "config_fetched": result["config_fetched"],
    }
