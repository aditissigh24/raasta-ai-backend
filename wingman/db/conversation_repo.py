"""
Wingman conversation repository.

All Postgres queries for the wingman pipeline.
NOTE: Message row INSERT is intentionally absent — the socket-server (Node.js)
      writes Message rows when it routes ai:response to the user room.
      This service only reads messages and manages Conversation counters / beat state.
"""
import logging
from typing import Optional

from langchain_core.messages import HumanMessage, AIMessage

from config.pg_client import pg_client

logger = logging.getLogger(__name__)


async def load_full(conversation_id: str, include_history_limit: int = 12) -> Optional[dict]:
    """
    Load everything needed for one turn in a single DB round-trip batch.

    Returns a dict with nested `character`, `scenario`, `current_beat` sub-dicts
    plus mutable conversation counters and recent message history.
    Returns None if conversation not found.
    """
    if not pg_client.is_available:
        return None

    try:
        async with pg_client._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    conv.id              AS conv_id,
                    conv."userId"        AS user_id,
                    conv."scenarioId"    AS scenario_id,
                    conv."currentBeatId" AS current_beat_id,
                    conv."currentBeatNumber",
                    conv."turnsInCurrentBeat",
                    conv."totalTurns",
                    conv."engagementScore",
                    conv."lastEvalAt",
                    conv.status,

                    u.name               AS u_name,
                    u."ageRange"         AS u_age_range,
                    u.gender             AS u_gender,

                    s.id                       AS s_id,
                    s."scenarioTitle",
                    s."settingDescription",
                    s.atmosphere,
                    s.tone,
                    s."timeOfDay",
                    s."overallArc",

                    c.id                AS c_id,
                    c.name              AS c_name,
                    c.age               AS c_age,
                    c.city              AS c_city,
                    c.archetype         AS c_archetype,
                    c."vibeSummary"     AS c_vibe_summary,
                    c.backstory         AS c_backstory,
                    c."speakingStyle"   AS c_speaking_style,
                    c."emojiUsage"      AS c_emoji_usage,
                    c."textingSpeed"    AS c_texting_speed,
                    c."voicePrompt"     AS c_voice_prompt,
                    c."hardLimits"      AS c_hard_limits,

                    sb.id                       AS sb_id,
                    sb."beatNumber"             AS sb_beat_number,
                    sb."beatType"               AS sb_beat_type,
                    sb."narrativeContext"        AS sb_narrative_context,
                    sb."characterEmotionalState" AS sb_emotional_state,
                    sb."flowDirective"           AS sb_flow_directive,
                    sb."hookDirective"           AS sb_hook_directive,
                    sb."minTurnsInBeat"          AS sb_min_turns,
                    sb."engagedAdvanceScore"     AS sb_advance_score

                FROM "Conversation" conv
                JOIN "User"      u  ON conv."userId"       = u.id
                JOIN "Scenario"  s  ON conv."scenarioId"   = s.id
                JOIN "Character" c  ON s."characterId"     = c.id
                LEFT JOIN "ScenarioBeat" sb ON conv."currentBeatId" = sb.id
                WHERE conv.id = $1
                """,
                conversation_id,
            )

        if not row:
            return None

        user_profile = {
            "name":      row["u_name"] or "",
            "age_range": row["u_age_range"] or "",
            "gender":    str(row["u_gender"]).lower() if row["u_gender"] else "",
        }

        character = {
            "id":            row["c_id"],
            "name":          row["c_name"],
            "age":           row["c_age"],
            "city":          row["c_city"],
            "archetype":     row["c_archetype"],
            "vibe_summary":  row["c_vibe_summary"],
            "backstory":     row["c_backstory"],
            "speaking_style": row["c_speaking_style"],
            "emoji_usage":   row["c_emoji_usage"],
            "texting_speed": row["c_texting_speed"],
            "voice_prompt":  row["c_voice_prompt"],
            "hard_limits":   list(row["c_hard_limits"] or []),
        }

        scenario = {
            "id":                  row["s_id"],
            "scenario_title":      row["scenarioTitle"],
            "setting_description": row["settingDescription"],
            "atmosphere":          row["atmosphere"],
            "tone":                row["tone"],
            "time_of_day":         row["timeOfDay"],
            "overall_arc":         row["overallArc"],
        }

        current_beat: Optional[dict] = None
        if row["sb_id"]:
            current_beat = {
                "id":                      row["sb_id"],
                "beat_number":             row["sb_beat_number"],
                "beat_type":               row["sb_beat_type"],
                "narrative_context":       row["sb_narrative_context"],
                "character_emotional_state": row["sb_emotional_state"],
                "flow_directive":          row["sb_flow_directive"],
                "hook_directive":          row["sb_hook_directive"],
                "min_turns_in_beat":       row["sb_min_turns"],
                "engaged_advance_score":   float(row["sb_advance_score"] or 3.5),
            }

        # Fetch message history
        history: list = []
        if include_history_limit > 0:
            history = await _fetch_history(conversation_id, include_history_limit)

        return {
            "conversation_id":       conversation_id,
            "user_id":               row["user_id"],
            "user_profile":          user_profile,
            "scenario_id":           row["scenario_id"],
            "character":             character,
            "scenario":              scenario,
            "current_beat":          current_beat or {},
            "turns_in_current_beat": row["turnsInCurrentBeat"],
            "total_turns":           row["totalTurns"],
            "engagement_score":      float(row["engagementScore"] or 3.0),
            "last_eval_at":          row["lastEvalAt"],
            "history":               history,
        }

    except Exception as e:
        logger.error(f"load_full({conversation_id}) failed: {e}", exc_info=True)
        return None


async def _fetch_history(conversation_id: str, limit: int) -> list:
    """Return last `limit` messages as LangChain message objects."""
    if not pg_client.is_available:
        return []
    try:
        async with pg_client._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT text, "senderType"
                FROM "Message"
                WHERE "conversationId" = $1
                  AND "deletedAt" IS NULL
                ORDER BY "createdAt" DESC
                LIMIT $2
                """,
                conversation_id,
                limit,
            )
        # rows are newest-first; reverse for chronological order
        result = []
        for r in reversed(rows):
            text = r["text"] or ""
            if r["senderType"] == "USER":
                result.append(HumanMessage(content=text))
            else:
                result.append(AIMessage(content=text))
        return result
    except Exception as e:
        logger.warning(f"_fetch_history({conversation_id}) failed: {e}")
        return []


async def increment_turns(conversation_id: str) -> bool:
    """Increment totalTurns and turnsInCurrentBeat by 1."""
    if not pg_client.is_available:
        return False
    try:
        async with pg_client._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE "Conversation"
                SET "totalTurns"         = "totalTurns" + 1,
                    "turnsInCurrentBeat" = "turnsInCurrentBeat" + 1,
                    "lastMessageAt"      = NOW()
                WHERE id = $1
                """,
                conversation_id,
            )
        return True
    except Exception as e:
        logger.warning(f"increment_turns({conversation_id}) failed: {e}")
        return False


async def update_engagement(
    conversation_id: str,
    score: float,
    last_eval_at: int,
) -> bool:
    """Persist engagement score and lastEvalAt turn marker."""
    if not pg_client.is_available:
        return False
    try:
        async with pg_client._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE "Conversation"
                SET "engagementScore" = $2,
                    "lastEvalAt"      = $3
                WHERE id = $1
                """,
                conversation_id,
                score,
                last_eval_at,
            )
        return True
    except Exception as e:
        logger.warning(f"update_engagement({conversation_id}) failed: {e}")
        return False


async def advance_beat(
    conversation_id: str,
    new_beat_id: str,
    new_beat_number: int,
) -> bool:
    """Advance to the next beat and reset turnsInCurrentBeat."""
    if not pg_client.is_available:
        return False
    try:
        async with pg_client._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE "Conversation"
                SET "currentBeatId"      = $2,
                    "currentBeatNumber"  = $3,
                    "turnsInCurrentBeat" = 0
                WHERE id = $1
                """,
                conversation_id,
                new_beat_id,
                new_beat_number,
            )
        return True
    except Exception as e:
        logger.warning(f"advance_beat({conversation_id}) failed: {e}")
        return False


async def get_next_beat(scenario_id: str, current_beat_number: int) -> Optional[dict]:
    """Return the next ScenarioBeat, or None if this is the final beat."""
    if not pg_client.is_available:
        return None
    try:
        async with pg_client._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    id, "beatNumber", "beatType",
                    "narrativeContext", "characterEmotionalState",
                    "flowDirective", "hookDirective",
                    "minTurnsInBeat", "engagedAdvanceScore"
                FROM "ScenarioBeat"
                WHERE "scenarioId"  = $1
                  AND "beatNumber"  = $2
                  AND "isActive"    = true
                """,
                scenario_id,
                current_beat_number + 1,
            )
        if not row:
            return None
        return {
            "id":                      row["id"],
            "beat_number":             row["beatNumber"],
            "beat_type":               row["beatType"],
            "narrative_context":       row["narrativeContext"],
            "character_emotional_state": row["characterEmotionalState"],
            "flow_directive":          row["flowDirective"],
            "hook_directive":          row["hookDirective"],
            "min_turns_in_beat":       row["minTurnsInBeat"],
            "engaged_advance_score":   float(row["engagedAdvanceScore"]),
        }
    except Exception as e:
        logger.warning(f"get_next_beat({scenario_id}, {current_beat_number}) failed: {e}")
        return None


async def complete_conversation(conversation_id: str) -> bool:
    """Mark conversation as COMPLETED."""
    if not pg_client.is_available:
        return False
    try:
        async with pg_client._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE "Conversation"
                SET status    = 'COMPLETED',
                    "endedAt" = NOW()
                WHERE id = $1
                """,
                conversation_id,
            )
        return True
    except Exception as e:
        logger.warning(f"complete_conversation({conversation_id}) failed: {e}")
        return False


async def get_last_n_user_messages(conversation_id: str, n: int = 3) -> list[str]:
    """Return text of last N USER messages for engagement eval input."""
    if not pg_client.is_available:
        return []
    try:
        async with pg_client._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT text
                FROM "Message"
                WHERE "conversationId" = $1
                  AND "senderType"     = 'USER'
                  AND "deletedAt"      IS NULL
                ORDER BY "createdAt" DESC
                LIMIT $2
                """,
                conversation_id,
                n,
            )
        return [r["text"] for r in reversed(rows) if r["text"]]
    except Exception as e:
        logger.warning(f"get_last_n_user_messages({conversation_id}) failed: {e}")
        return []
