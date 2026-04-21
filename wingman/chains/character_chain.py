"""
Character chain — DeepSeek primary with Claude Sonnet streaming fallback.

stream_character_response() is an async generator that yields text chunks.
It tries DeepSeek first (DEEPSEEK_TIMEOUT_SECONDS). On timeout or error,
it falls back to Claude Sonnet — also streaming, so the user sees tokens
immediately after the fallback activates.
"""
import asyncio
import logging
from typing import AsyncGenerator

from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, BaseMessage

from config.settings import settings

logger = logging.getLogger(__name__)

# Module-level LLM instances (constructed once, reused across requests)
_deepseek: ChatOpenAI | None = None
_claude_sonnet: ChatAnthropic | None = None


def _get_deepseek() -> ChatOpenAI:
    global _deepseek
    if _deepseek is None:
        _deepseek = ChatOpenAI(
            model="deepseek-chat",
            base_url=settings.DEEPSEEK_BASE_URL,
            api_key=settings.DEEPSEEK_API_KEY,
            temperature=0.85,
            max_tokens=120,
            streaming=True,
        )
    return _deepseek


def _get_claude_sonnet() -> ChatAnthropic:
    global _claude_sonnet
    if _claude_sonnet is None:
        _claude_sonnet = ChatAnthropic(
            model="claude-sonnet-4-5",
            api_key=settings.ANTHROPIC_API_KEY,
            temperature=0.85,
            max_tokens=120,
        )
    return _claude_sonnet


def _build_messages(
    system_prompt: str,
    history: list[BaseMessage],
    user_message: str,
) -> list[BaseMessage]:
    # Drop the last history entry if it's the same human message that was
    # already written to DB by the socket-server before this request arrived.
    # Without this, the LLM sees the user message twice (history + user_message),
    # which causes LangChain to concatenate consecutive HumanMessages.
    clean_history = list(history)
    if clean_history and isinstance(clean_history[-1], HumanMessage):
        if clean_history[-1].content == user_message:
            clean_history.pop()
    return [SystemMessage(content=system_prompt), *clean_history, HumanMessage(content=user_message)]


async def stream_character_response(
    system_prompt: str,
    history: list[BaseMessage],
    user_message: str,
) -> AsyncGenerator[str, None]:
    """
    Stream the character's reply token-by-token.

    Normal path:  DeepSeek streams within DEEPSEEK_TIMEOUT_SECONDS.
    Fallback path: Claude Sonnet streams after DeepSeek fails/times out.
    """
    messages = _build_messages(system_prompt, history, user_message)

    try:
        logger.info(f"character_chain: DeepSeek → streaming start (timeout={settings.DEEPSEEK_TIMEOUT_SECONDS}s)")
        async with asyncio.timeout(settings.DEEPSEEK_TIMEOUT_SECONDS):
            async for chunk in _get_deepseek().astream(messages):
                content = chunk.content
                if content:
                    yield content
        logger.info("character_chain: DeepSeek → streaming done")
        return  # DeepSeek completed successfully

    except asyncio.TimeoutError:
        logger.warning(
            f"character_chain: DeepSeek timed out after {settings.DEEPSEEK_TIMEOUT_SECONDS}s "
            "→ falling back to Claude Sonnet"
        )
    except Exception as e:
        logger.warning(f"character_chain: DeepSeek error ({e}) → falling back to Claude Sonnet")

    # Fallback: Claude Sonnet (also streams)
    try:
        logger.info("character_chain: Claude Sonnet fallback → streaming start")
        async for chunk in _get_claude_sonnet().astream(messages):
            content = chunk.content
            if content:
                yield content
        logger.info("character_chain: Claude Sonnet fallback → streaming done")
    except Exception as e:
        logger.error(f"character_chain: Claude Sonnet fallback also failed: {e}", exc_info=True)
        raise
