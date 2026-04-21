"""
Redis session cache for Wingman conversation context.

prime_session_cache()  — called once on roleplay_start; stores everything in Redis
update_ctx_cache()     — atomic Lua-based merge update; called by background tasks
load_ctx_cache()       — Redis GET used by load_context node
"""
import json
import logging
import asyncio
from typing import Optional

from config.redis_client import redis_client
from config.settings import settings

logger = logging.getLogger(__name__)

# Redis key templates
CTX_KEY = "wingman:ctx:{conv_id}"
STABLE_PROMPT_KEY = "wingman:prompt:stable:{char_id}:{scenario_id}"

CTX_TTL = 86400      # 24 h
PROMPT_TTL = 600     # 10 min

# Lua script embedded as a string (same content as ctx_update.lua)
_LUA_CTX_UPDATE = """
local key = KEYS[1]
local updates_json = ARGV[1]
local ttl = tonumber(ARGV[2])
local raw = redis.call('GET', key)
if not raw then
    return nil
end
local ctx = cjson.decode(raw)
local updates = cjson.decode(updates_json)
for k, v in pairs(updates) do
    ctx[k] = v
end
redis.call('SET', key, cjson.encode(ctx), 'EX', ttl)
return 1
"""


async def prime_session_cache(
    conversation_id: str,
    conversation_data: dict,
    stable_prompt: Optional[str] = None,
) -> None:
    """
    Store full conversation context in Redis after roleplay_start DB fetch.

    conversation_data should be the dict returned by conversation_repo.load_full().
    stable_prompt (if provided) is also cached under the stable-prompt key.
    """
    if not redis_client.is_available:
        return

    ctx = {
        "character":             conversation_data.get("character", {}),
        "scenario":              conversation_data.get("scenario", {}),
        "current_beat":          conversation_data.get("current_beat", {}),
        "turns_in_current_beat": conversation_data.get("turns_in_current_beat", 0),
        "total_turns":           conversation_data.get("total_turns", 0),
        "engagement_score":      conversation_data.get("engagement_score", 3.0),
        "user_profile":          conversation_data.get("user_profile", {}),
        "suggested_hook":        None,
        "inject_hook":           _compute_inject_hook(
            conversation_data.get("engagement_score", 3.0),
            conversation_data.get("current_beat", {}),
        ),
    }

    await redis_client.set(
        CTX_KEY.format(conv_id=conversation_id),
        json.dumps(ctx),
        ex=CTX_TTL,
    )

    if stable_prompt and conversation_data.get("character") and conversation_data.get("scenario"):
        char_id = conversation_data["character"].get("id", "")
        scen_id = conversation_data["scenario"].get("id", "")
        if char_id and scen_id:
            await redis_client.set(
                STABLE_PROMPT_KEY.format(char_id=char_id, scenario_id=scen_id),
                stable_prompt,
                ex=PROMPT_TTL,
            )

    logger.debug(f"Session cache primed for conv={conversation_id}")


async def load_ctx_cache(conversation_id: str) -> Optional[dict]:
    """Return the cached context dict, or None on miss."""
    if not redis_client.is_available:
        return None
    raw = await redis_client.get(CTX_KEY.format(conv_id=conversation_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


async def get_stable_prompt(character_id: str, scenario_id: str) -> Optional[str]:
    """Return the cached assembled stable prompt string, or None on miss."""
    if not redis_client.is_available:
        return None
    return await redis_client.get(
        STABLE_PROMPT_KEY.format(char_id=character_id, scenario_id=scenario_id)
    )


async def set_stable_prompt(character_id: str, scenario_id: str, prompt: str) -> None:
    """Cache the assembled stable prompt string."""
    if not redis_client.is_available:
        return
    await redis_client.set(
        STABLE_PROMPT_KEY.format(char_id=character_id, scenario_id=scenario_id),
        prompt,
        ex=PROMPT_TTL,
    )


async def update_ctx_cache(conversation_id: str, updates: dict) -> None:
    """
    Atomically merge `updates` into the cached context using a Lua script.

    If the key has been evicted, re-prime from DB automatically.
    """
    if not redis_client.is_available or not redis_client._client:
        return

    try:
        result = await redis_client._client.eval(
            _LUA_CTX_UPDATE,
            1,
            CTX_KEY.format(conv_id=conversation_id),
            json.dumps(updates),
            str(CTX_TTL),
        )

        if result is None:
            # Key was evicted mid-session — re-prime from DB
            logger.info(
                f"CTX key evicted for conv={conversation_id}, re-priming from DB"
            )
            await _reprime_from_db(conversation_id)

    except Exception as e:
        logger.warning(f"update_ctx_cache({conversation_id}) failed: {e}")


async def _reprime_from_db(conversation_id: str) -> None:
    """Re-load conversation data from DB and restore the Redis context."""
    try:
        from wingman.db import conversation_repo
        data = await conversation_repo.load_full(conversation_id, include_history_limit=0)
        if data:
            await prime_session_cache(conversation_id, data)
    except Exception as e:
        logger.warning(f"_reprime_from_db({conversation_id}) failed: {e}")


def _compute_inject_hook(engagement_score: float, current_beat: dict) -> bool:
    """Return True if engagement score is below the beat's advance threshold."""
    advance_score = current_beat.get("engaged_advance_score", 1.5)
    return engagement_score < advance_score
