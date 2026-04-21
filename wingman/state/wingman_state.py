from typing import TypedDict, Optional, Annotated
from langgraph.graph.message import add_messages


class ConversationState(TypedDict):
    """
    State passed through pipeline nodes for one conversation turn.
    All fields are written by nodes and read by downstream nodes/pipeline.
    """

    # Identity
    conversation_id: str
    user_id: str
    user_message: str
    user_profile: dict

    # DB records — loaded by load_context, Redis-first
    character: dict
    scenario: dict
    current_beat: dict
    turns_in_current_beat: int
    total_turns: int

    # Engagement
    engagement_score: float
    inject_hook: bool
    suggested_hook: Optional[str]

    # Conversation history (LangChain message objects)
    messages: Annotated[list, add_messages]

    # Assembled prompt sections (built by assemble_prompt)
    stable_section: str
    dynamic_section: str
    system_prompt: str

    # Output
    raw_response: Optional[str]
    final_response: Optional[str]
    model_used: str
    was_engagement_triggered: bool
    langfuse_trace_id: str


class BackgroundState(TypedDict):
    """
    State for background_graph — runs after the response is sent to the user.
    Never blocks the user-facing path.
    """
    conversation_id: str
    total_turns: int
    turns_in_current_beat: int
    engagement_score: float
    current_beat: dict
    scenario_id: str
    last_n_user_messages: list[str]
    character_name: str
    scenario_title: str
    langfuse_trace_id: str

    # Outputs
    new_engagement_score: Optional[float]
    suggested_hook: Optional[str]
    beat_advanced: bool
    conversation_completed: bool
