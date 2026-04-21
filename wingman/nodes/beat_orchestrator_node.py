"""
beat_orchestrator_node — advances the scenario beat when conditions are met.

Runs in background_graph after engagement_eval_node.
Checks min_turns_in_beat AND engagement score threshold.
If both pass, fetches the next beat and updates Postgres + Redis.
"""
import logging

from wingman.state.wingman_state import BackgroundState
from wingman.db import conversation_repo
from wingman.db.ctx_cache import update_ctx_cache

logger = logging.getLogger(__name__)


async def beat_orchestrator_node(state: BackgroundState) -> dict:
    """
    Advance to the next beat if conditions are met.

    Conditions:
      1. turns_in_current_beat >= beat.min_turns_in_beat
      2. engagement_score >= beat.engaged_advance_score
    """
    conv_id = state["conversation_id"]
    beat    = state.get("current_beat", {})

    # Use updated score from eval if available, else carry forward; default 0.0 if both are None
    score = state.get("new_engagement_score") or state.get("engagement_score") or 0.0

    min_turns    = beat.get("min_turns_in_beat", 3)
    advance_score = beat.get("engaged_advance_score", 3.5)
    beat_turns   = state["turns_in_current_beat"]

    logger.info(
        f"beat_orchestrator: checking advance | conv={conv_id} "
        f"beat={beat.get('beat_number', '?')} "
        f"turns={beat_turns}/{min_turns} score={score:.1f}/{advance_score}"
    )

    should_advance = beat_turns >= min_turns and score >= advance_score

    if not should_advance:
        logger.info(
            f"beat_orchestrator: no advance — conditions not met | conv={conv_id} "
            f"(need turns>={min_turns} AND score>={advance_score})"
        )
        return {"beat_advanced": False, "conversation_completed": False}

    # Fetch next beat
    logger.info(f"beat_orchestrator: conditions met — fetching next beat | conv={conv_id}")
    next_beat = await conversation_repo.get_next_beat(
        scenario_id=state.get("scenario_id", ""),
        current_beat_number=beat.get("beat_number", 1),
    )

    # Final beat completed → mark conversation done
    if next_beat is None:
        await conversation_repo.complete_conversation(conv_id)
        await update_ctx_cache(conv_id, {
            "current_beat":          beat,
            "turns_in_current_beat": 0,
        })
        logger.info(f"beat_orchestrator: CONVERSATION COMPLETED (final beat done) | conv={conv_id}")
        return {"beat_advanced": True, "conversation_completed": True}

    # Advance to next beat
    await conversation_repo.advance_beat(
        conversation_id=conv_id,
        new_beat_id=next_beat["id"],
        new_beat_number=next_beat["beat_number"],
    )

    # Update Redis cache — new beat, reset beat turn counter
    await update_ctx_cache(conv_id, {
        "current_beat":          next_beat,
        "turns_in_current_beat": 0,
    })

    logger.info(
        f"beat_orchestrator: advanced to beat {next_beat['beat_number']} "
        f"({next_beat.get('beat_type', '')}) | conv={conv_id} — DB + Redis updated"
    )
    return {"beat_advanced": True, "conversation_completed": False}
