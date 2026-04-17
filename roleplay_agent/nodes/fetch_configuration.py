"""
Node for fetching user config, character data, and scenario data from the backend.
"""
import asyncio
import logging
from roleplay_agent.state import RoleplayState
from services.backend_client import backend_client

logger = logging.getLogger(__name__)


async def fetch_configuration_node(state: RoleplayState) -> dict:
    """
    Fetch user config, character data, and scenario data in parallel from the backend.

    Skipped if config has already been fetched for this session.

    Args:
        state: Current conversation state.

    Returns:
        Updated state dict with user_config, character_data, scenario_data,
        and config_fetched=True.
    """
    if state.get("config_fetched", False):
        return {}

    user_id = state["user_config"].get("user_id", "")
    character_id = state.get("character_id", "")
    scenario_id = state.get("scenario_id", "")

    # Fetch all three in parallel
    user_config_task = _fetch_user_config(user_id)
    character_task = _fetch_character(character_id)
    scenario_task = _fetch_scenario(scenario_id)

    user_config, character_data, scenario_data = await asyncio.gather(
        user_config_task, character_task, scenario_task
    )

    return {
        "user_config": user_config,
        "character_data": character_data,
        "scenario_data": scenario_data,
        "config_fetched": True,
    }


async def _fetch_user_config(user_id: str) -> dict:
    if not user_id:
        return {"user_id": "unknown", "name": "Friend", "gender": "unknown", "age": 25}
    try:
        config = await backend_client.fetch_user_config(user_id)
        return dict(config)
    except Exception as e:
        logger.warning(f"Failed to fetch user config for {user_id}: {e}")
        return {"user_id": user_id, "name": "Friend", "gender": "unknown", "age": 25}


async def _fetch_character(character_id: str) -> dict:
    if not character_id:
        return {}
    try:
        return await backend_client.fetch_character(character_id)
    except Exception as e:
        logger.warning(f"Failed to fetch character {character_id}: {e}")
        return {"char_id": character_id}


async def _fetch_scenario(scenario_id: str) -> dict:
    if not scenario_id:
        return {}
    try:
        return await backend_client.fetch_scenario(scenario_id)
    except Exception as e:
        logger.warning(f"Failed to fetch scenario {scenario_id}: {e}")
        return {"scenario_id": scenario_id}
