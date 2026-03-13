"""
State schema for the coach conversation agent.
"""
from typing import TypedDict, Annotated
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class UserConfig(TypedDict):
    """User configuration from backend."""
    user_id: str
    name: str
    gender: str
    age: int


class CoachState(TypedDict):
    """
    State schema for the coach agent conversation.
    
    Attributes:
        messages: Conversation history with automatic message accumulation
        user_config: User information fetched from backend
        coach_type: The selected coach persona
        session_id: Unique session identifier
        is_first_message: Whether this is the first message in the session
        config_fetched: Whether user config has been fetched
        session_summaries: List of past session summaries for context
    """
    messages: Annotated[list[BaseMessage], add_messages]
    user_config: UserConfig
    coach_type: str
    session_id: str
    is_first_message: bool
    config_fetched: bool
    session_summaries: list[str]


