"""
State schema for the onboarding conversation agent.

Tracks the user's progress through info collection (name, age) and
chapter/scenario selection before entering the roleplay session.
"""
from typing import TypedDict, Optional


class OnboardingState(TypedDict):
    """
    State for one turn of the onboarding agent.

    This state is persisted in Redis between turns as a JSON blob.
    Each turn, the saved fields are loaded, new user_message is injected,
    the graph runs to END, and the updated fields are saved back.

    Steps (current_step progression):
        "init"              → first contact, no message yet
        "awaiting_name"     → greeting sent, waiting for user's name
        "awaiting_age"      → name collected, waiting for user's age
        "awaiting_chapter"  → age collected + chapters shown, waiting for chapter selection
        "awaiting_scenario" → chapter selected + scenarios shown, waiting for scenario selection
        "complete"          → scenario selected, onboarding done, roleplay can begin
    """

    # --- Identity ---
    user_id: str
    user_config: dict

    # --- Flow control ---
    current_step: str

    # --- Collected data ---
    collected_name: Optional[str]
    collected_age: Optional[int]
    selected_chapter_id: Optional[str]
    selected_chapter_name: Optional[str]
    selected_scenario_id: Optional[str]
    selected_character_id: Optional[str]

    # --- Cached backend data (avoid re-fetching within a session) ---
    chapters: list          # [{id, name, description, scenarioCount}]
    scenarios: list         # [{scenario_id, char_id, scenario_title, ...}]

    # --- Current turn input ---
    latest_user_message: Optional[str]

    # --- Current turn output (set by nodes, read by agent runner) ---
    reply_text: str
    options_to_send: Optional[list]   # list of {id, label, subtext?} dicts for chips
    onboarding_complete: bool
