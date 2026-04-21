"""
Recovery chain — Claude Sonnet direct streaming with Anthropic prefix caching.

Used on the engagement recovery path (inject_hook=True).
Streams directly to the user — no rephraser in the streaming path.
The stable_section prefix is marked with cache_control="ephemeral" so
Anthropic caches it server-side (5 min TTL, refreshed on each hit).
"""
import logging
from typing import AsyncGenerator

import anthropic

from config.settings import settings
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage

logger = logging.getLogger(__name__)

_anthropic_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic(
            api_key=settings.ANTHROPIC_API_KEY,
        )
    return _anthropic_client


def _to_anthropic_messages(
    history: list[BaseMessage],
    user_message: str,
) -> list[dict]:
    """Convert LangChain messages + user message to Anthropic message list."""
    result = []
    for msg in history:
        if isinstance(msg, HumanMessage) or (hasattr(msg, "type") and msg.type == "human"):
            result.append({"role": "user", "content": msg.content})
        else:
            result.append({"role": "assistant", "content": msg.content})

    # Drop the last entry if it's the same human message already in DB history.
    if result and result[-1]["role"] == "user" and result[-1]["content"] == user_message:
        result.pop()

    result.append({"role": "user", "content": user_message})
    return result


async def stream_recovery_response(
    stable_section: str,
    dynamic_section: str,
    history: list[BaseMessage],
    user_message: str,
) -> AsyncGenerator[str, None]:
    """
    Stream Claude Sonnet's recovery response with Anthropic prefix caching.

    stable_section is marked cache_control=ephemeral so Anthropic caches
    the guardrails + character + scene prefix (refreshed every hit, 5-min TTL).
    """
    client = _get_client()
    messages = _to_anthropic_messages(history, user_message)

    try:
        async with client.messages.stream(
            model="claude-sonnet-4-5",
            max_tokens=120,  # capped — recovery responses should be punchy
            temperature=0.9,
            system=[
                {
                    "type": "text",
                    "text": stable_section,
                    "cache_control": {"type": "ephemeral"},  # Anthropic prefix cache
                },
                {
                    "type": "text",
                    "text": dynamic_section,
                },
            ],
            messages=messages,
        ) as stream:
            async for text in stream.text_stream:
                if text:
                    yield text
    except Exception as e:
        logger.error(f"Claude Sonnet recovery stream failed: {e}", exc_info=True)
        raise
