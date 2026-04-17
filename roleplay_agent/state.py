"""
State schema for the roleplay conversation agent.
"""
from typing import TypedDict, Annotated
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class RoleplayState(TypedDict):
    """
    State schema for the roleplay agent conversation.

    Attributes:
        messages: Conversation history with automatic message accumulation.
        user_config: User information fetched from backend (name, gender, age, etc.).
        character_data: Character record from backend (char_id, name, age, city, archetype,
                        vibe_summary, backstory, speaking_style, emoji_usage, texting_speed).
        scenario_data: Scenario record from backend (scenario_id, char_id, chapter,
                       chapter_name, scenario_title, difficulty, situation_setup_for_user,
                       learning_objective, good_outcome, bad_outcome, primal_hook).
        character_id: Character identifier (e.g. "C01").
        scenario_id: Scenario identifier (e.g. "S01").
        session_id: Unique session identifier.
        is_first_message: Whether this is the first exchange in the session.
        config_fetched: Whether character/scenario/user config has been fetched.
        session_summaries: List of past session summary texts for continuity context.
    """

    messages: Annotated[list[BaseMessage], add_messages]
    user_config: dict
    character_data: dict
    scenario_data: dict
    character_id: str
    scenario_id: str
    session_id: str
    is_first_message: bool
    config_fetched: bool
    session_summaries: list[str]
