"""
load_context node — Redis-first, Postgres fallback.

Reads the full conversation context from Redis (set on roleplay_start).
Falls back to a direct DB query if Redis misses (eviction, restart, first turn).
"""
import logging
from typing import Optional

from wingman.state.wingman_state import ConversationState
from wingman.db import ctx_cache, conversation_repo
from config.settings import settings

logger = logging.getLogger(__name__)


async def load_context(state: ConversationState) -> dict:
    """
    Return context fields merged into ConversationState.

    Hot path:  Redis GET (~0.5ms)
    Cold path: Postgres query + cache reprime (~150ms)
    """
    conv_id = state["conversation_id"]

    # 1. Try Redis first
    ctx = await ctx_cache.load_ctx_cache(conv_id)
    if ctx:
        logger.info(f"load_context: Redis hit | conv={conv_id} — fetching history")
        history = await conversation_repo._fetch_history(conv_id, settings.MAX_HISTORY_TURNS)
        logger.info(
            f"load_context: done (Redis path) | conv={conv_id} "
            f"history_msgs={len(history)} beat={ctx.get('current_beat', {}).get('beat_number', '?')}"
        )
        return {
            "character":             ctx["character"],
            "scenario":              ctx["scenario"],
            "current_beat":          ctx["current_beat"],
            "turns_in_current_beat": ctx["turns_in_current_beat"],
            "total_turns":           ctx["total_turns"],
            "engagement_score":      ctx["engagement_score"],
            "user_profile":          ctx.get("user_profile", {}),
            "suggested_hook":        ctx.get("suggested_hook"),
            "inject_hook":           ctx.get("inject_hook", False),
            "messages":              history,
            "langfuse_trace_id":     _make_trace_id(conv_id),
        }

    # 2. Cold path — DB fallback
    logger.info(f"load_context: Redis miss — falling back to DB | conv={conv_id}")
    data = await conversation_repo.load_full(conv_id, include_history_limit=settings.MAX_HISTORY_TURNS)

    if not data:
        raise RuntimeError(f"Conversation {conv_id} not found in DB")

    logger.info(
        f"load_context: DB data fetched | conv={conv_id} "
        f"char='{data.get('character', {}).get('name', '?')}' "
        f"history_msgs={len(data.get('history', []))} — re-priming Redis cache"
    )

    # Re-prime the cache so subsequent turns are fast
    await ctx_cache.prime_session_cache(conv_id, data)
    logger.info(f"load_context: Redis cache re-primed | conv={conv_id}")

    inject_hook = ctx_cache._compute_inject_hook(
        data["engagement_score"], data["current_beat"]
    )

    return {
        "character":             data["character"],
        "scenario":              data["scenario"],
        "current_beat":          data["current_beat"],
        "turns_in_current_beat": data["turns_in_current_beat"],
        "total_turns":           data["total_turns"],
        "engagement_score":      data["engagement_score"],
        "user_profile":          data.get("user_profile", {}),
        "suggested_hook":        None,
        "inject_hook":           inject_hook,
        "messages":              data["history"],
        "langfuse_trace_id":     _make_trace_id(conv_id),
    }


def _make_trace_id(conversation_id: str) -> str:
    import uuid
    return f"wingman-{conversation_id[:8]}-{uuid.uuid4().hex[:8]}"
