"""
save_output node — background task after the user has received the response.

Only increments Conversation turn counters in Postgres.
Message rows are written by the socket-server (Node.js) when it routes
ai:response to the user room — this service must NOT duplicate that write.
"""
import asyncio
import logging

from wingman.state.wingman_state import ConversationState
from wingman.db import conversation_repo
from wingman.db.ctx_cache import update_ctx_cache

logger = logging.getLogger(__name__)


async def save_output(state: ConversationState) -> dict:
    """
    Persist turn counter increments and refresh the Redis context cache.

    Runs as a background task — never blocks the user-facing response.
    """
    conv_id = state["conversation_id"]

    try:
        # 1. Increment DB counters
        await conversation_repo.increment_turns(conv_id)

        # 2. Atomically update Redis cache with new counts
        new_total = state["total_turns"] + 1
        new_beat_turns = state["turns_in_current_beat"] + 1

        await update_ctx_cache(conv_id, {
            "total_turns":           new_total,
            "turns_in_current_beat": new_beat_turns,
        })

        logger.info(
            f"save_output: DB + Redis updated | conv={conv_id} "
            f"total_turns={new_total} beat_turns={new_beat_turns}"
        )

    except Exception as e:
        logger.error(f"save_output({conv_id}) failed: {e}", exc_info=True)

    return {}
