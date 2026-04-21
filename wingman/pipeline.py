"""
Wingman pipeline — main entry point for the socket worker.

Public API:
    prime_session_cache()      — call on roleplay_start; pre-warms Redis
    handle_turn_streaming()    — async generator; yields text chunks, fires background tasks

Architecture:
    Phase 1 (blocking, ~1-5ms with Redis hot):
        load_context + assemble_prompt — no LLM calls
    Phase 2 (streaming to user):
        stream LLM tokens via character_chain or recovery_chain
    Phase 3 (background, asyncio.create_task):
        save_output + background_graph (eval + beat advance)

Per-conversation asyncio.Lock serialises turns so rapid double-sends
don't read stale context. Uses WeakValueDictionary to prevent memory leaks.
"""
import asyncio
import logging
import uuid
from typing import AsyncGenerator
from weakref import WeakValueDictionary

from wingman.state.wingman_state import ConversationState, BackgroundState
from wingman.nodes.load_context import load_context
from wingman.nodes.assemble_prompt import assemble_prompt
from wingman.nodes.save_output import save_output
from wingman.chains.character_chain import stream_character_response
from wingman.chains.recovery_chain import stream_recovery_response
from wingman.db import conversation_repo
from wingman.db import ctx_cache as _ctx_cache
from wingman.graphs.background_graph import background_graph

logger = logging.getLogger(__name__)

# Per-conversation locks — WeakValueDictionary auto-cleans idle conversations
_conv_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _get_conv_lock(conversation_id: str) -> asyncio.Lock:
    lock = _conv_locks.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _conv_locks[conversation_id] = lock
    return lock


# ---------------------------------------------------------------------------
# Session pre-warming (called on roleplay_start)
# ---------------------------------------------------------------------------

async def prime_session_cache(conversation_id: str) -> None:
    """
    Fetch full conversation context from DB and store in Redis.
    Also pre-builds the stable prompt section if possible.

    Call once on roleplay_start so the first real message hits Redis, not Postgres.
    """
    try:
        data = await conversation_repo.load_full(conversation_id, include_history_limit=0)
        if not data:
            logger.warning(f"prime_session_cache: conv={conversation_id} not found in DB")
            return

        # Try to pre-build stable prompt while we have the data
        stable_prompt: str | None = None
        try:
            # Build a minimal state so assemble_prompt can run
            dummy_state: ConversationState = {
                "conversation_id": conversation_id,
                "user_id":         data.get("user_id", ""),
                "user_message":    "",
                "user_profile":    data.get("user_profile", {}),
                "character":       data["character"],
                "scenario":        data["scenario"],
                "current_beat":    data["current_beat"],
                "turns_in_current_beat": data["turns_in_current_beat"],
                "total_turns":     data["total_turns"],
                "engagement_score": data["engagement_score"],
                "inject_hook":     False,
                "suggested_hook":  None,
                "messages":        [],
                "stable_section":  "",
                "dynamic_section": "",
                "system_prompt":   "",
                "raw_response":    None,
                "final_response":  None,
                "model_used":      "",
                "was_engagement_triggered": False,
                "langfuse_trace_id": "",
            }
            prompt_result = await assemble_prompt(dummy_state)
            stable_prompt = prompt_result.get("stable_section")
        except Exception as e:
            logger.debug(f"prime_session_cache: stable prompt pre-build failed: {e}")

        await _ctx_cache.prime_session_cache(conversation_id, data, stable_prompt)
        logger.info(f"Session cache primed for conv={conversation_id}")

    except Exception as e:
        logger.warning(f"prime_session_cache({conversation_id}) failed: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Main streaming entry point
# ---------------------------------------------------------------------------

async def handle_turn_streaming(
    conversation_id: str,
    user_message: str,
    user_id: str = "",
) -> AsyncGenerator[str, None]:
    """
    Async generator — yields text chunks as they arrive from the LLM.

    Acquires a per-conversation lock so rapid double-sends are serialised.
    Fires save_output + background_graph as a background task after streaming.
    """
    async with _get_conv_lock(conversation_id):
        state = await _run_pre_llm_nodes(conversation_id, user_message, user_id)

        full_response = ""
        inject_hook = state.get("inject_hook", False)

        async for chunk in _stream_llm(state, inject_hook):
            full_response += chunk
            yield chunk

        logger.info(
            f"[Phase 3/3] LLM stream → done  | conv={conversation_id} "
            f"response_len={len(full_response)}"
        )

    # Lock released — post-turn tasks run outside the lock
    asyncio.create_task(
        _post_turn_tasks(state, full_response),
        name=f"post-turn-{conversation_id[:8]}",
    )


async def _run_pre_llm_nodes(
    conversation_id: str,
    user_message: str,
    user_id: str,
) -> ConversationState:
    """Phase 1: load_context → assemble_prompt (no LLM, ~1-5ms with Redis hot)."""

    # Minimal bootstrap state needed by load_context
    bootstrap: ConversationState = {
        "conversation_id":      conversation_id,
        "user_id":              user_id,
        "user_message":         user_message,
        "user_profile":         {},
        # Remaining fields will be populated by load_context
        "character":            {},
        "scenario":             {},
        "current_beat":         {},
        "turns_in_current_beat": 0,
        "total_turns":          0,
        "engagement_score":     3.0,
        "inject_hook":          False,
        "suggested_hook":       None,
        "messages":             [],
        "stable_section":       "",
        "dynamic_section":      "",
        "system_prompt":        "",
        "raw_response":         None,
        "final_response":       None,
        "model_used":           "",
        "was_engagement_triggered": False,
        "langfuse_trace_id":    "",
    }

    # load_context: Redis-first, DB fallback
    logger.info(f"[Phase 1/3] load_context → start | conv={conversation_id}")
    ctx_update = await load_context(bootstrap)
    state = {**bootstrap, **ctx_update}
    logger.info(
        f"[Phase 1/3] load_context → done  | conv={conversation_id} "
        f"char='{state['character'].get('name', '?')}' "
        f"beat={state['current_beat'].get('beat_number', '?')} "
        f"turn={state['total_turns']} score={state['engagement_score']}"
    )

    # assemble_prompt: stable from Redis, dynamic fresh
    logger.info(f"[Phase 2/3] assemble_prompt → start | conv={conversation_id}")
    prompt_update = await assemble_prompt(state)
    state = {**state, **prompt_update}
    logger.info(
        f"[Phase 2/3] assemble_prompt → done  | conv={conversation_id} "
        f"system_prompt_len={len(state['system_prompt'])} "
        f"inject_hook={state.get('inject_hook', False)}"
    )

    return state


async def _stream_llm(
    state: ConversationState,
    inject_hook: bool,
) -> AsyncGenerator[str, None]:
    """Phase 2: stream tokens from the appropriate chain."""
    conv_id = state["conversation_id"]
    if inject_hook:
        logger.info(f"[Phase 3/3] LLM stream → start | conv={conv_id} model=claude-sonnet (recovery/hook path)")
        async for chunk in stream_recovery_response(
            stable_section=state["stable_section"],
            dynamic_section=state["dynamic_section"],
            history=state["messages"],
            user_message=state["user_message"],
        ):
            yield chunk
    else:
        logger.info(f"[Phase 3/3] LLM stream → start | conv={conv_id} model=deepseek (normal path)")
        async for chunk in stream_character_response(
            system_prompt=state["system_prompt"],
            history=state["messages"],
            user_message=state["user_message"],
        ):
            yield chunk


async def _post_turn_tasks(state: ConversationState, final_response: str) -> None:
    """Phase 3: save turn counters + run background graph."""
    conv_id = state["conversation_id"]
    try:
        logger.info(f"[BG 1/3] save_output → start | conv={conv_id}")
        await save_output(state)
        logger.info(f"[BG 1/3] save_output → done  | conv={conv_id}")

        logger.info(f"[BG 2/3] fetching last N user messages | conv={conv_id}")
        last_n_messages = await conversation_repo.get_last_n_user_messages(
            conv_id, n=3
        )
        logger.info(f"[BG 2/3] last N messages fetched | conv={conv_id} count={len(last_n_messages)}")

        bg_state: BackgroundState = {
            "conversation_id":       conv_id,
            "total_turns":           state["total_turns"] + 1,
            "turns_in_current_beat": state["turns_in_current_beat"] + 1,
            "engagement_score":      state["engagement_score"] or 0.0,
            "current_beat":          state["current_beat"],
            "scenario_id":           state["scenario"].get("id", ""),
            "last_n_user_messages":  last_n_messages,
            "character_name":        state["character"].get("name", ""),
            "scenario_title":        state["scenario"].get("scenario_title", ""),
            "langfuse_trace_id":     state["langfuse_trace_id"],
            "new_engagement_score":  None,
            "suggested_hook":        state.get("suggested_hook"),
            "beat_advanced":         False,
            "conversation_completed": False,
        }

        logger.info(f"[BG 3/3] background_graph → start | conv={conv_id}")
        await background_graph.ainvoke(bg_state)
        logger.info(f"[BG 3/3] background_graph → done  | conv={conv_id}")

    except Exception as e:
        logger.error(
            f"_post_turn_tasks({conv_id}) failed: {e}", exc_info=True
        )
