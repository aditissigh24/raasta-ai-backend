"""
Shared helpers for AI request processing.

The Redis pub/sub integration has been replaced by a direct Socket.IO connection
(see services/socket_worker.py). This module retains the reusable utility
functions that are still referenced by other parts of the application.
"""
import logging
import asyncio
import uuid
import httpx
from typing import Optional, Dict, List

from langchain_core.messages import HumanMessage, AIMessage

from config.prompts.roleplay_prompt import character_initiates, get_character_opening
from services.backend_client import backend_client
from services.meta_pixel_client import meta_pixel_client
from services.mixpanel_client import mixpanel_client
from config.settings import settings
from config.redis_client import redis_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session management (used by the /session REST endpoint in main.py)
# ---------------------------------------------------------------------------

async def get_or_create_session_id(
    user_uid: str, character_id: str, scenario_id: str
) -> tuple[str, bool]:
    """
    Return the session ID for a user + character + scenario triplet.

    Creates a new UUID session the first time the combination is seen, then
    returns the same ID on every subsequent call until it expires (24 h TTL,
    reset on each access).

    Returns:
        (session_id, was_created)
    """
    redis_key = (
        f"raasta:session:user:{user_uid}:char:{character_id}:scene:{scenario_id}"
    )
    existing = await redis_client.get(redis_key)
    if existing:
        await redis_client.set(redis_key, existing, ex=86400)
        return existing, False

    session_id = str(uuid.uuid4())
    await redis_client.set(redis_key, session_id, ex=86400)

    active_key = f"{character_id}:{scenario_id}"
    await redis_client.sadd_user_active_coach(user_uid, active_key)

    logger.info(
        f"New session created for {user_uid}+{character_id}+{scenario_id}: "
        f"{session_id[:30]}..."
    )
    return session_id, True


def has_missing_fields(user_config: dict) -> bool:
    """Return True if user_config contains placeholder / default values."""
    name = user_config.get("name", "")
    if not name or name == "Friend":
        return True
    gender = user_config.get("gender", "")
    if not gender or gender == "unknown":
        return True
    age = user_config.get("age", 25)
    if age in (25, 0):
        return True
    return False


# ---------------------------------------------------------------------------
# Opening message helpers
# ---------------------------------------------------------------------------

async def generate_character_opening_message(
    character_id: str,
    scenario_id: str,
) -> str:
    """
    Return the opening message for a character-initiates scenario.

    Tries to use a hardcoded message from the prompt module first.
    Falls back to fetching the character data and using a generic opener.
    """
    if not character_initiates(scenario_id):
        return ""

    opening = get_character_opening(scenario_id, {})
    if opening:
        return opening

    try:
        char_data = await backend_client.fetch_character(character_id)
        return get_character_opening(scenario_id, char_data)
    except Exception as e:
        logger.warning(f"Could not fetch character for opening: {e}")
        return "hey"


# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------

async def store_event_in_mongodb(
    event_name: str,
    collection_name: str,
    event_properties: Dict,
    distinct_id: Optional[str] = None,
) -> bool:
    """Store an event in MongoDB via the event storage API."""
    try:
        api_keys = settings.EVENT_API_KEYS.split(",")
        if not api_keys or not api_keys[0].strip():
            logger.warning("No API keys configured for MongoDB event storage")
            return False

        api_key = api_keys[0].strip()
        payload = {
            "event_name": event_name,
            "collection_name": collection_name,
            "event_properties": event_properties,
        }
        if distinct_id:
            payload["distinct_id"] = distinct_id

        async with httpx.AsyncClient() as client:
            response = await client.post(
                settings.EVENT_API_URL,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": api_key,
                },
                timeout=10.0,
            )
            if response.status_code == 201:
                result = response.json()
                logger.info(
                    f"Event stored in MongoDB: {event_name} "
                    f"(ID: {result.get('event_id')})"
                )
                return True
            else:
                logger.error(
                    f"Failed to store event in MongoDB: "
                    f"{response.status_code} - {response.text}"
                )
                return False
    except Exception as e:
        logger.error(f"Error storing event in MongoDB: {e}", exc_info=True)
        return False


async def _fire_conversation_analytics_events(
    sender_uid: str,
    receiver_uid: str,
    session_id: str,
    conversation_id: str,
    character_id: str,
    existing_user_msg_count: int,
) -> None:
    """
    Fire chat_started / chat_engaged analytics events.

    - existing_user_msg_count == 0  →  chat_started
    - existing_user_msg_count == 2  →  chat_engaged
    """
    if existing_user_msg_count not in (0, 2):
        return

    user_data_dict: dict = {}
    try:
        user_config = await backend_client.fetch_user_config(sender_uid)

        phone = user_config.get("phone", "")
        country_code = user_config.get("countryCode", "")
        if phone and country_code and phone.startswith(country_code.replace("+", "")):
            phone = phone[len(country_code.replace("+", "")):]

        full_name = user_config.get("name", "")
        name_parts = full_name.split(" ", 1) if full_name else []
        first_name = name_parts[0] if len(name_parts) > 0 else ""
        last_name = name_parts[1] if len(name_parts) > 1 else ""

        gender = user_config.get("gender", "")
        if gender:
            gender_lower = gender.lower()
            if "male" in gender_lower and "female" not in gender_lower:
                gender = "m"
            elif "female" in gender_lower:
                gender = "f"
            else:
                gender = ""

        user_data_dict = {
            "email": user_config.get("email", ""),
            "phone": phone,
            "first_name": first_name,
            "last_name": last_name,
            "gender": gender,
            "country": "in",
        }
        user_data_dict = {k: v for k, v in user_data_dict.items() if v}
    except Exception as e:
        logger.warning(f"Failed to fetch user data for analytics event: {e}")

    if existing_user_msg_count == 0:
        logger.info(f"Firing Chat Started event for conversation {conversation_id}")
        asyncio.create_task(
            meta_pixel_client.send_chat_started_event(
                user_id=sender_uid,
                session_id=session_id,
                coach_type=character_id,
                conversation_id=conversation_id,
                user_data=user_data_dict,
            )
        )
        asyncio.create_task(
            mixpanel_client.send_chat_started_event(
                user_id=sender_uid,
                session_id=session_id,
                coach_type=character_id,
                conversation_id=conversation_id,
            )
        )
        asyncio.create_task(
            store_event_in_mongodb(
                event_name="CHAT_STARTED",
                collection_name="roleplay_events",
                event_properties={
                    "user_id": sender_uid,
                    "session_id": session_id,
                    "character_id": character_id,
                    "conversation_id": conversation_id,
                    "message_count": 1,
                    "receiver_uid": receiver_uid,
                },
                distinct_id=sender_uid,
            )
        )
    elif existing_user_msg_count == 2:
        logger.info(f"Firing Chat Engaged event for conversation {conversation_id}")
        asyncio.create_task(
            meta_pixel_client.send_chat_engaged_event(
                user_id=sender_uid,
                session_id=session_id,
                coach_type=character_id,
                conversation_id=conversation_id,
                user_data=user_data_dict,
            )
        )
        asyncio.create_task(
            mixpanel_client.send_chat_engaged_event(
                user_id=sender_uid,
                session_id=session_id,
                coach_type=character_id,
                conversation_id=conversation_id,
            )
        )
        asyncio.create_task(
            store_event_in_mongodb(
                event_name="CHAT_ENGAGED",
                collection_name="roleplay_events",
                event_properties={
                    "user_id": sender_uid,
                    "session_id": session_id,
                    "character_id": character_id,
                    "conversation_id": conversation_id,
                    "message_count": 3,
                    "receiver_uid": receiver_uid,
                },
                distinct_id=sender_uid,
            )
        )


# ---------------------------------------------------------------------------
# Message conversion
# ---------------------------------------------------------------------------

def convert_db_messages_to_langchain(messages: List[Dict]) -> List:
    """
    Convert DB message records to LangChain message objects.

    Records are expected to have:
        senderType: "USER" | "AI_COACH" | "HUMAN_COACH" | any non-USER value
        text: str
    """
    langchain_messages = []
    for msg in messages:
        text = msg.get("text", "")
        if not text:
            continue
        sender_type = msg.get("senderType", "USER")
        if sender_type == "USER":
            langchain_messages.append(HumanMessage(content=text))
        else:
            langchain_messages.append(AIMessage(content=text))
    return langchain_messages


# ---------------------------------------------------------------------------
# User profile analysis (background task, fills missing profile fields)
# ---------------------------------------------------------------------------

async def trigger_user_analysis(
    user_id: str,
    user_config: dict,
    message_text: str,
) -> None:
    """Background task: extract missing profile fields from the user's message."""
    try:
        from roleplay_agent.nodes.analyze_user_details import (
            analyze_user_details,
            get_missing_fields,
        )

        missing_fields = get_missing_fields(user_config)
        if not missing_fields:
            logger.info(f"No missing profile fields for user {user_id}, skipping analysis")
            return

        logger.info(f"Analysing message for user {user_id}, missing: {missing_fields}")
        extracted_data = await analyze_user_details(
            user_message=message_text,
            user_config=user_config,
            missing_fields=missing_fields,
        )
        if not extracted_data:
            return

        logger.info(f"Extracted data for {user_id}: {extracted_data}")
        success = await backend_client.update_user_details(user_id, extracted_data)
        if success:
            logger.info(f"Updated backend profile for user {user_id}")
        else:
            logger.warning(f"Failed to update backend profile for user {user_id}")

    except Exception as e:
        logger.error(f"Error in user analysis for {user_id}: {e}", exc_info=True)
