"""
engagement_eval_node — scores user engagement using Claude Haiku.

Runs in background_graph every EVAL_INTERVAL_TURNS turns.
Result is persisted to Postgres and the Redis context cache.
"""
import logging

from wingman.state.wingman_state import BackgroundState
from wingman.chains.eval_chain import run_eval
from wingman.prompts import langfuse_loader
from wingman.db import conversation_repo
from wingman.db.ctx_cache import update_ctx_cache, _compute_inject_hook

logger = logging.getLogger(__name__)


async def engagement_eval_node(state: BackgroundState) -> dict:
    """
    Score the last N user messages and persist the result.

    Returns new_engagement_score, suggested_hook, and updated inject_hook flag.
    """
    conv_id = state["conversation_id"]
    messages = state.get("last_n_user_messages", [])

    if not messages:
        logger.info(f"engagement_eval: no messages to evaluate — skipping | conv={conv_id}")
        return {
            "new_engagement_score": state["engagement_score"],
            "suggested_hook":       state.get("suggested_hook"),
        }

    logger.info(f"engagement_eval: start | conv={conv_id} msg_count={len(messages)}")

    # Build eval system prompt from Langfuse template
    try:
        logger.info(f"engagement_eval: fetching eval template from Langfuse | conv={conv_id}")
        eval_tpl = await langfuse_loader.get("wingman/eval/engagement-scorer")
        beat = state.get("current_beat", {})
        eval_system = eval_tpl.compile({
            "character_name":    state.get("character_name", ""),
            "scenario_title":    state.get("scenario_title", ""),
            "beat_type":         beat.get("beat_type", ""),
            "narrative_context": beat.get("narrative_context", ""),
            "n":                 str(len(messages)),
        })
        logger.info(f"engagement_eval: eval template compiled | conv={conv_id}")
    except Exception as e:
        logger.warning(f"engagement_eval: template build failed: {e} | conv={conv_id}")
        return {"new_engagement_score": state["engagement_score"], "suggested_hook": None}

    # Format messages for eval
    messages_text = "\n".join(
        f"{i + 1}. {msg}" for i, msg in enumerate(messages)
    )

    # Run eval
    logger.info(f"engagement_eval: running Claude Haiku eval | conv={conv_id}")
    result = await run_eval(eval_system, messages_text)
    if not result:
        logger.warning(f"engagement_eval: eval returned None | conv={conv_id}")
        return {"new_engagement_score": state["engagement_score"], "suggested_hook": None}

    score = result["score"]
    hook  = result.get("suggested_hook")

    logger.info(
        f"engagement_eval: done | conv={conv_id} score={score:.1f} "
        f"reason='{result.get('reason', '')[:60]}'"
    )

    # Persist to DB
    logger.info(f"engagement_eval: persisting score to DB | conv={conv_id} score={score:.1f}")
    await conversation_repo.update_engagement(
        conversation_id=conv_id,
        score=score,
        last_eval_at=state["total_turns"],
    )
    logger.info(f"engagement_eval: DB updated | conv={conv_id}")

    # Update Redis cache atomically
    inject = _compute_inject_hook(score, state.get("current_beat", {}))
    await update_ctx_cache(conv_id, {
        "engagement_score": score,
        "suggested_hook":   hook,
        "inject_hook":      inject,
    })
    logger.info(
        f"engagement_eval: Redis cache updated | conv={conv_id} "
        f"inject_hook={inject} suggested_hook={bool(hook)} → next: beat_orchestrator"
    )

    return {
        "new_engagement_score": score,
        "suggested_hook":       hook,
    }
