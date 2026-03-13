"""
Redis pub/sub message handler for AI coach responses.
Subscribes to raasta:ai:request, runs LLM, publishes to raasta:ai:response.
"""
import json
import logging
import asyncio
import time
import uuid
import httpx
from typing import Optional, Dict, List

from langchain_core.messages import HumanMessage, AIMessage

from coach_agent.agent import run_coach_agent
from services.backend_client import backend_client
from services.task_manager import create_background_task
from services.meta_pixel_client import meta_pixel_client
from services.mixpanel_client import mixpanel_client
from config.settings import settings
from config.redis_client import redis_client

logger = logging.getLogger(__name__)


async def get_or_create_session_id(user_uid: str, coach_uid: str) -> tuple[str, bool]:
    """
    Return the session ID for a user+coach pair, creating one if it doesn't exist.

    The Redis key is raasta:session:user:{user_uid}:coach:{coach_uid} so each user+coach
    pair gets its own independent session. The TTL is reset to 24 h on every access
    so that an active conversation never expires mid-session.

    Also records the coach in the raasta:active_coaches:{user_uid} set so that the
    disconnect handler knows which coaches to summarise for.

    Returns:
        (session_id, was_created) -- was_created is True only on first creation.
    """
    redis_key = f"raasta:session:user:{user_uid}:coach:{coach_uid}"
    existing = await redis_client.get(redis_key)
    if existing:
        await redis_client.set(redis_key, existing, ex=86400)
        return existing, False
    session_id = str(uuid.uuid4())
    await redis_client.set(redis_key, session_id, ex=86400)
    await redis_client.sadd_user_active_coach(user_uid, coach_uid)
    logger.info(f"📝 New session created for {user_uid}+{coach_uid}: {session_id[:30]}...")
    return session_id, True


def has_missing_fields(user_config: dict) -> bool:
    """Check if user_config has any missing or default fields."""
    name = user_config.get("name", "")
    if not name or name == "Friend":
        return True
    gender = user_config.get("gender", "")
    if not gender or gender == "unknown":
        return True
    age = user_config.get("age", 25)
    if age == 25 or age == 0:
        return True
    current_situation = user_config.get("currentSituation", "")
    if not current_situation:
        return True
    situations = user_config.get("situations", [])
    if not situations:
        return True
    return False


_COACH_GREETINGS = {
    "kabir": "Hey! Main Kabir hoon. Batao, kya chal raha hai? I'm here to help you figure things out.",
    "tara": "Hi! I'm Tara. I'm here to help you navigate what you're going through. Tell me, what's on your mind?",
    "vikram": "Hey, Vikram here. Whatever you're dealing with, let's sort it out. What's going on?",
}


def generate_coach_greeting(coach_type: str) -> str:
    """Return the hardcoded greeting for a coach."""
    return _COACH_GREETINGS.get(
        coach_type,
        "Hello, I'm your coach. Tell me what's on your mind.",
    )


async def store_event_in_mongodb(
    event_name: str,
    collection_name: str,
    event_properties: Dict,
    distinct_id: Optional[str] = None
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
            "event_properties": event_properties
        }
        if distinct_id:
            payload["distinct_id"] = distinct_id

        async with httpx.AsyncClient() as client:
            response = await client.post(
                settings.EVENT_API_URL,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": api_key
                },
                timeout=10.0
            )
            if response.status_code == 201:
                result = response.json()
                logger.info(f"✅ Event stored in MongoDB: {event_name} (ID: {result.get('event_id')})")
                return True
            else:
                logger.error(f"❌ Failed to store event in MongoDB: {response.status_code} - {response.text}")
                return False
    except Exception as e:
        logger.error(f"❌ Error storing event in MongoDB: {e}", exc_info=True)
        return False


async def _fire_conversation_analytics_events(
    sender_uid: str,
    receiver_uid: str,
    session_id: str,
    conversation_id: str,
    coach_type_for_event: str,
    existing_user_msg_count: int,
) -> None:
    """
    Fire chat_started / chat_engaged analytics events.

    - existing_user_msg_count == 0  ->  chat_started
    - existing_user_msg_count == 2  ->  chat_engaged
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
        user_data_dict = {}

    if existing_user_msg_count == 0:
        logger.info(f"🎯 Firing Chat Started event for conversation {conversation_id}")
        asyncio.create_task(
            meta_pixel_client.send_chat_started_event(
                user_id=sender_uid,
                session_id=session_id,
                coach_type=coach_type_for_event,
                conversation_id=conversation_id,
                user_data=user_data_dict,
            )
        )
        asyncio.create_task(
            mixpanel_client.send_chat_started_event(
                user_id=sender_uid,
                session_id=session_id,
                coach_type=coach_type_for_event,
                conversation_id=conversation_id,
            )
        )
        asyncio.create_task(
            store_event_in_mongodb(
                event_name="CHAT_STARTED",
                collection_name="coach_events",
                event_properties={
                    "user_id": sender_uid,
                    "session_id": session_id,
                    "coach_type": coach_type_for_event,
                    "conversation_id": conversation_id,
                    "message_count": 1,
                    "receiver_uid": receiver_uid,
                },
                distinct_id=sender_uid,
            )
        )
    elif existing_user_msg_count == 2:
        logger.info(f"🎯 Firing Chat Engaged event for conversation {conversation_id}")
        asyncio.create_task(
            meta_pixel_client.send_chat_engaged_event(
                user_id=sender_uid,
                session_id=session_id,
                coach_type=coach_type_for_event,
                conversation_id=conversation_id,
                user_data=user_data_dict,
            )
        )
        asyncio.create_task(
            mixpanel_client.send_chat_engaged_event(
                user_id=sender_uid,
                session_id=session_id,
                coach_type=coach_type_for_event,
                conversation_id=conversation_id,
            )
        )
        asyncio.create_task(
            store_event_in_mongodb(
                event_name="CHAT_ENGAGED",
                collection_name="coach_events",
                event_properties={
                    "user_id": sender_uid,
                    "session_id": session_id,
                    "coach_type": coach_type_for_event,
                    "conversation_id": conversation_id,
                    "message_count": 3,
                    "receiver_uid": receiver_uid,
                },
                distinct_id=sender_uid,
            )
        )


def convert_db_messages_to_langchain(messages: List[Dict]) -> List:
    """
    Convert backend DB message format to LangChain message format.

    Backend messages have:
        senderType: "USER" | "AI_COACH" | "HUMAN_COACH"
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


async def trigger_user_analysis(
    user_id: str,
    user_config: dict,
    message_text: str
) -> None:
    """
    Trigger background analysis to extract missing user details from message.
    Runs in parallel with the main coach response flow.
    """
    try:
        from coach_agent.nodes.analyze_user_details import analyze_user_details, get_missing_fields

        missing_fields = get_missing_fields(user_config)
        if not missing_fields:
            logger.info(f"🔍 No missing fields for user {user_id}, skipping analysis")
            return

        logger.info(f"🔍 Analyzing message for user {user_id}, missing fields: {missing_fields}")
        extracted_data = await analyze_user_details(
            user_message=message_text,
            user_config=user_config,
            missing_fields=missing_fields
        )
        if not extracted_data:
            logger.info(f"🔍 No user details extracted from message for {user_id}")
            return

        logger.info(f"🔍 Extracted data for {user_id}: {extracted_data}")
        backend_success = await backend_client.update_user_details(user_id, extracted_data)
        if backend_success:
            logger.info(f"✅ Successfully updated backend for user {user_id}")
        else:
            logger.warning(f"⚠️ Failed to update backend for user {user_id}")

    except Exception as e:
        logger.error(f"❌ Error in user analysis for {user_id}: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Core AI request handler (called per ai:request message)
# ---------------------------------------------------------------------------

async def process_ai_request(data: dict) -> Optional[str]:
    """
    Process an incoming raasta:ai:request message and return the AI reply text.

    Expected data keys:
        roomId, conversationId, coachId, sessionId, userId,
        text (or messageText)

    Returns the AI reply text, or None on failure.
    """
    room_id = data.get("roomId")
    conversation_id = data.get("conversationId")
    coach_id = data.get("coachId", "")
    session_id = data.get("sessionId")
    user_id = data.get("userId")
    message_text = data.get("text") or data.get("messageText", "")

    if not all([user_id, message_text, coach_id]):
        logger.warning(f"Missing required fields in raasta:ai:request: {data}")
        return None

    logger.info(f"💬 Processing ai:request from {user_id} to coach {coach_id}: {message_text[:80]}...")

    try:
        # Resolve coach type from coachId (int DB id, numeric string, or name)
        coach_type = await backend_client.resolve_coach_type(coach_id)

        if not coach_type:
            logger.warning(f"Could not determine coach type for {coach_id}, defaulting to kabir")
            coach_type = "kabir"

        logger.info(f"Resolved coach type: {coach_type}")

        # Kill switch
        if not settings.AI_RESPONSES_ENABLED:
            logger.info(f"🔕 AI responses disabled for user {user_id}")
            return None

        # Fetch conversation history from backend DB
        history_messages: List[Dict] = []
        if session_id:
            try:
                history_messages = await backend_client.fetch_messages_by_session(session_id)
                logger.info(f"Fetched {len(history_messages)} messages from DB for session {session_id[:30]}...")
            except Exception as e:
                logger.warning(f"Failed to fetch message history: {e}")

        # First-message greeting check
        if len(history_messages) <= 1:
            logger.info(f"🎉 First message to coach {coach_type} — sending greeting")
            return generate_coach_greeting(coach_type)

        # Convert DB messages to LangChain format (exclude the latest user message
        # since run_coach_agent receives it separately via user_message param)
        existing_messages = convert_db_messages_to_langchain(
            history_messages[:-1] if history_messages else []
        )

        # Fetch user configuration
        try:
            user_config = await backend_client.fetch_user_config(user_id)
            user_config_dict = dict(user_config)
            logger.info(f"Fetched user config for {user_id}: {user_config_dict.get('name')}")
        except Exception as e:
            logger.warning(f"Failed to fetch user config: {e}. Using defaults.")
            user_config_dict = {
                "user_id": user_id,
                "name": "",
                "gender": "unknown",
                "age": 25
            }

        # Background user analysis
        if has_missing_fields(user_config_dict):
            asyncio.create_task(
                trigger_user_analysis(
                    user_id=user_id,
                    user_config=user_config_dict,
                    message_text=message_text
                )
            )

        # Fetch session summaries for context
        session_summaries: List[str] = []
        try:
            summaries_data = await backend_client.fetch_recent_session_summaries(
                user_id, conversation_id or "", limit=3
            )
            session_summaries = [s.get("summaryText", "") for s in summaries_data if s.get("summaryText")]
            if session_summaries:
                logger.info(f"📚 Loaded {len(session_summaries)} session summaries for context")
        except Exception as e:
            logger.warning(f"Failed to fetch session summaries: {e}")

        # Fire analytics events
        try:
            existing_user_msg_count = await backend_client.fetch_user_message_count(
                room_id or ""
            )
            await _fire_conversation_analytics_events(
                sender_uid=user_id,
                receiver_uid=coach_id,
                session_id=session_id or "",
                conversation_id=conversation_id or "",
                coach_type_for_event=coach_type,
                existing_user_msg_count=existing_user_msg_count,
            )
        except Exception as e:
            logger.warning(f"Analytics events failed: {e}")

        # Generate AI response
        logger.info(f"Generating AI response with coach {coach_type}")
        result = await run_coach_agent(
            session_id=conversation_id or "",
            user_id=user_id,
            coach_type=coach_type,
            user_message=message_text,
            existing_messages=existing_messages,
            user_config=user_config_dict,
            config_fetched=True,
            session_summaries=session_summaries
        )

        ai_response = result["response"]
        logger.info(f"✅ AI response generated: {ai_response[:100]}...")
        return ai_response

    except asyncio.CancelledError:
        logger.warning(f"⚠️ AI request cancelled for user {user_id}")
        raise
    except Exception as e:
        logger.error(f"❌ Error processing ai:request: {e}", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Disconnect handler (called per ai:disconnect message)
# ---------------------------------------------------------------------------

async def handle_ai_disconnect(data: dict) -> None:
    """
    Handle a user disconnect event from the socket server.

    Expected data keys: userId, coachId, sessionId
    """
    from services.session_summarizer import summarize_and_store_session

    user_id = data.get("userId")
    coach_id = data.get("coachId")
    session_id = data.get("sessionId")

    if not user_id:
        logger.warning("ai:disconnect missing userId")
        return

    logger.info(f"🔌 ai:disconnect for user {user_id}, coach {coach_id}")

    if session_id:
        logger.info(f"🔚 Summarising session {session_id[:30]}...")
        await create_background_task(
            user_id=user_id,
            coro=summarize_and_store_session(user_id, session_id),
            task_name=f"summarize-{user_id}-{coach_id or 'unknown'}"
        )
    else:
        # Fall back to looking up all active coaches for this user
        coach_uids = await redis_client.smembers_user_active_coaches(user_id)
        if not coach_uids:
            logger.warning(f"⚠️ No active sessions found for user {user_id}")
            return

        GRACE_PERIOD_SECONDS = 180
        for c_uid in coach_uids:
            redis_key = f"raasta:session:user:{user_id}:coach:{c_uid}"
            sid = await redis_client.get(redis_key)
            if not sid:
                continue
            logger.info(f"🔚 Summarising session {sid[:30]}... for {user_id}+{c_uid}")
            await create_background_task(
                user_id=user_id,
                coro=summarize_and_store_session(user_id, sid),
                task_name=f"summarize-{user_id}-{c_uid}"
            )
            await redis_client.set(redis_key, sid, ex=GRACE_PERIOD_SECONDS)

        await redis_client.delete_user_active_coaches(user_id)


# ---------------------------------------------------------------------------
# Redis pub/sub subscriber loop
# ---------------------------------------------------------------------------

async def start_redis_subscriber() -> None:
    """
    Subscribe to raasta:ai:request and raasta:ai:disconnect Redis channels.
    For each raasta:ai:request, run the LLM and publish the reply to raasta:ai:response.
    For each raasta:ai:disconnect, trigger session summarization.

    This function runs forever and should be launched as a background task
    on application startup.
    """
    while True:
        try:
            pubsub = await redis_client.subscribe(["raasta:ai:request", "raasta:ai:disconnect"])
            if pubsub is None:
                logger.error("Failed to subscribe to Redis channels, retrying in 5s...")
                await asyncio.sleep(5)
                continue

            logger.info("📡 Redis subscriber active on raasta:ai:request & raasta:ai:disconnect")

            async for raw_message in pubsub.listen():
                if raw_message["type"] != "message":
                    continue

                channel = raw_message["channel"]
                try:
                    data = json.loads(raw_message["data"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning(f"Invalid JSON on {channel}: {raw_message['data']}")
                    continue

                if channel == "raasta:ai:request":
                    asyncio.create_task(_handle_ai_request_message(data))
                elif channel == "raasta:ai:disconnect":
                    asyncio.create_task(handle_ai_disconnect(data))

        except asyncio.CancelledError:
            logger.info("Redis subscriber cancelled")
            raise
        except Exception as e:
            logger.error(f"Redis subscriber error: {e}", exc_info=True)
            await asyncio.sleep(5)


async def _handle_ai_request_message(data: dict) -> None:
    """Wrapper that processes an ai:request and publishes the response."""
    try:
        reply_text = await process_ai_request(data)

        if reply_text:
            response_payload = {
                "roomId": data.get("roomId"),
                "conversationId": data.get("conversationId"),
                "coachId": data.get("coachId"),
                "sessionId": data.get("sessionId"),
                "replyText": reply_text,
            }
            await redis_client.publish("raasta:ai:response", response_payload)
            logger.info(f"📤 Published raasta:ai:response for room {data.get('roomId')}")
        else:
            logger.warning(f"No reply generated for raasta:ai:request from {data.get('userId')}")

    except Exception as e:
        logger.error(f"❌ Error handling raasta:ai:request: {e}", exc_info=True)
