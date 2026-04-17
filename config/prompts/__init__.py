"""Prompts package."""
from .roleplay_prompt import (
    get_character_prompt,
    get_character_opening,
    get_wingman_feedback_messages,
    character_initiates,
    WINGMAN_PROMPT,
)

__all__ = [
    "get_character_prompt",
    "get_character_opening",
    "get_wingman_feedback_messages",
    "character_initiates",
    "WINGMAN_PROMPT",
]


