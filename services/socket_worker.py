"""
Socket.IO AI Worker.

Connects to the socket-server as a privileged ``ai_worker`` client and drives all
real-time AI interactions:

  Listens for:
    ai:request   — user message (onboarding_start | onboarding | roleplay_start | roleplay)
    user:session — presence event (connected | disconnected)

  Emits back:
    ai:response  — main reply + optional side-effect payloads
                   (name_collected, age_collected, scenario_selected)

The socket-server routes each ai:response to the correct user room and handles
all Prisma/DB writes declared in the side-effect type fields.
"""
import asyncio
import json
import logging
from typing import Optional

import socketio

from config.settings import settings
from config.redis_client import redis_client
from config.pg_client import pg_client
from roleplay_agent.agent import run_roleplay_agent
from onboarding_agent.agent import run_onboarding_agent
from services.webhook_handler import convert_db_messages_to_langchain
from services.task_manager import create_background_task

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Socket.IO client (module-level singleton)
# ---------------------------------------------------------------------------

sio = socketio.AsyncClient(
    reconnection=True,
    reconnection_delay=1,
    reconnection_delay_max=10,
    logger=False,
    engineio_logger=False,
)

# Redis key for storing the active roleplay session per user so the disconnect
# handler knows which conversation to summarise.
_ACTIVE_SESSION_KEY = "raasta:roleplay:active:{user_id}"
_ACTIVE_SESSION_TTL = 86400  # 24 h


# ---------------------------------------------------------------------------
# Connection lifecycle events
# ---------------------------------------------------------------------------

@sio.event
async def connect():
    logger.info(f"AI worker connected to socket-server (sid={sio.get_sid()})")


@sio.event
async def connect_error(data):
    logger.error(f"AI worker connection failed: {data}")


@sio.event
async def disconnect():
    logger.warning("AI worker disconnected from socket-server")


# ---------------------------------------------------------------------------
# ai:request — route by type
# ---------------------------------------------------------------------------

@sio.on("ai:request")
async def on_ai_request(data: dict):
    """Receive an ai:request from the socket-server and dispatch to the correct handler."""
    if not isinstance(data, dict):
        logger.warning(f"ai:request received non-dict payload: {type(data)}")
        return

    req_type = data.get("type", "")
    user_id = data.get("userId", "?")
    message_id = data.get("messageId", "?")

    logger.info(
        f"ai:request | type={req_type} userId={user_id} messageId={message_id}"
    )

    if not settings.AI_RESPONSES_ENABLED:
        logger.info("AI responses disabled (kill-switch). Ignoring request.")
        return

    if req_type in ("onboarding_start", "onboarding"):
        asyncio.create_task(handle_onboarding(data))
    elif req_type in ("roleplay_start", "roleplay"):
        asyncio.create_task(handle_roleplay(data))
    else:
        logger.warning(f"Unknown ai:request type: '{req_type}' — ignoring")


# ---------------------------------------------------------------------------
# user:session — presence events
# ---------------------------------------------------------------------------

@sio.on("user:session")
async def on_user_session(data: dict):
    """Handle user presence events emitted by the socket-server."""
    if not isinstance(data, dict):
        return

    event = data.get("event", "")
    user_id = data.get("userId", "")
    logger.info(f"user:session | event={event} userId={user_id}")

    if event == "disconnected":
        asyncio.create_task(handle_user_disconnected(data))


# ---------------------------------------------------------------------------
# Onboarding handler
# ---------------------------------------------------------------------------

async def handle_onboarding(data: dict) -> None:
    """
    Process an onboarding_start or onboarding ai:request.

    Flow:
      1. Fetch user config from Postgres (best-effort).
      2. Run the onboarding LangGraph agent with the user's message.
      3. Emit side-effect events for any data collected this turn
         (name_collected, age_collected, scenario_selected).
      4. Emit the main ai:response (type onboarding or onboarding_complete).
    """
    user_id = data.get("userId", "")
    session_id = data.get("sessionId", "")
    message_id = data.get("messageId", "")
    room_id = data.get("roomId", "")
    is_start = data.get("type") == "onboarding_start"
    raw_text = data.get("text", "")

    if not user_id:
        logger.warning("handle_onboarding: missing userId — skipping")
        return

    try:
        # ----------------------------------------------------------------
        # Fetch user config (used to pre-fill name/age if already known)
        # ----------------------------------------------------------------
        user_config: dict = {"user_id": user_id, "name": "Friend", "age": 25}
        try:
            fetched = await pg_client.fetch_user(user_id)
            if fetched:
                user_config = fetched
        except Exception as e:
            logger.warning(f"Could not fetch user config for onboarding {user_id}: {e}")

        # ----------------------------------------------------------------
        # Determine the user message for this turn
        # ----------------------------------------------------------------
        # onboarding_start always has text "__START__" — treat as None (no user message)
        user_message: Optional[str] = None
        if not is_start and raw_text and raw_text != "__START__":
            user_message = raw_text

        # ----------------------------------------------------------------
        # Run onboarding agent
        # ----------------------------------------------------------------
        result = await run_onboarding_agent(
            user_id=user_id,
            user_config=user_config,
            user_message=user_message,
        )

        # ----------------------------------------------------------------
        # Emit side-effect events (DB writes delegated to socket-server)
        # ----------------------------------------------------------------
        step_completed = result.get("step_completed")
        step_data = result.get("step_data", {})

        if step_completed == "name_collected":
            await _emit(
                "ai:response",
                {
                    "type": "name_collected",
                    "userId": user_id,
                    "messageId": message_id,
                    "name": step_data.get("name"),
                },
            )

        elif step_completed == "age_collected":
            await _emit(
                "ai:response",
                {
                    "type": "age_collected",
                    "userId": user_id,
                    "messageId": message_id,
                    "ageRange": step_data.get("age"),
                },
            )

        elif step_completed == "chapter_selected":
            await _emit(
                "ai:response",
                {
                    "type": "chapter_selected",
                    "userId": user_id,
                    "messageId": message_id,
                    "chapterId": step_data.get("chapterId"),
                    "chapterName": step_data.get("chapterName"),
                },
            )

        # ----------------------------------------------------------------
        # Emit main ai:response
        # ----------------------------------------------------------------
        onboarding_complete = result.get("onboarding_complete", False)
        response_type = "onboarding_complete" if onboarding_complete else "onboarding"

        payload = {
            "type": response_type,
            "userId": user_id,
            "roomId": room_id,
            "messageId": message_id,
            "sessionId": session_id,
            "replyText": result.get("reply_text", ""),
            "options": result.get("options_to_send"),
            "onboardingComplete": onboarding_complete,
            "characterId": result.get("selected_character_id"),
            "scenarioId": result.get("selected_scenario_id"),
            "conversationId": None,
            "wingmanTip": None,
            "isUserMessage": False,
        }

        await _emit("ai:response", payload)
        logger.info(
            f"Onboarding response sent | userId={user_id} type={response_type} "
            f"complete={onboarding_complete}"
        )

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(
            f"handle_onboarding error for userId={user_id}: {e}", exc_info=True
        )


# ---------------------------------------------------------------------------
# Roleplay handler
# ---------------------------------------------------------------------------

async def handle_roleplay(data: dict) -> None:
    """
    Process a roleplay_start or roleplay ai:request.

    Flow:
      1. Pre-fetch user config, character, and scenario from Postgres.
      2. Fetch conversation message history from Postgres.
      3. Run the roleplay LangGraph agent.
      4. Emit ai:response with the character's reply + optional wingmanTip.
    """
    is_start = data.get("type") == "roleplay_start"
    user_id = data.get("userId", "")
    session_id = data.get("sessionId", "")
    message_id = data.get("messageId", "")
    room_id = data.get("roomId", "")
    conversation_id = data.get("conversationId", "")
    character_id = data.get("characterId", "")
    scenario_id = data.get("scenarioId", "")
    raw_text = data.get("text", "")

    if not user_id:
        logger.warning("handle_roleplay: missing userId — skipping")
        return

    try:
        # ----------------------------------------------------------------
        # On roleplay_start, record this as the user's active session so
        # the disconnect handler can summarise it later.
        # ----------------------------------------------------------------
        if is_start and conversation_id:
            active_data = json.dumps({
                "conversation_id": conversation_id,
                "session_id": session_id,
                "character_id": character_id,
                "scenario_id": scenario_id,
            })
            await redis_client.set(
                _ACTIVE_SESSION_KEY.format(user_id=user_id),
                active_data,
                ex=_ACTIVE_SESSION_TTL,
            )

        # ----------------------------------------------------------------
        # Pre-fetch config from Postgres in parallel
        # ----------------------------------------------------------------
        user_config_task = _safe_fetch_user(user_id)
        character_task = _safe_fetch_character(character_id)
        scenario_task = _safe_fetch_scenario(scenario_id)

        user_config, character_data, scenario_data = await asyncio.gather(
            user_config_task, character_task, scenario_task
        )

        # ----------------------------------------------------------------
        # Fetch conversation history from Postgres
        # ----------------------------------------------------------------
        history_messages = []
        if conversation_id:
            try:
                history_messages = await pg_client.fetch_messages_by_conversation(
                    conversation_id
                )
                logger.info(
                    f"Fetched {len(history_messages)} messages for conv={conversation_id}"
                )
            except Exception as e:
                logger.warning(f"Could not fetch message history: {e}")

        # For roleplay_start the history is empty; for roleplay we exclude
        # the latest user message since it's passed separately to the agent.
        existing_messages = convert_db_messages_to_langchain(
            history_messages[:-1] if history_messages and not is_start else []
        )

        # ----------------------------------------------------------------
        # Fetch recent session summaries for continuity context
        # ----------------------------------------------------------------
        session_summaries: list[str] = []
        try:
            summaries = await pg_client.fetch_recent_session_summaries(
                user_id, conversation_id, limit=3
            )
            session_summaries = [
                s.get("summaryText", "") for s in summaries if s.get("summaryText")
            ]
        except Exception as e:
            logger.warning(f"Could not fetch session summaries: {e}")

        # ----------------------------------------------------------------
        # Determine user message for this turn
        # ----------------------------------------------------------------
        # roleplay_start: text is "__START__" — character sends the opening line
        user_message: Optional[str] = None
        if not is_start and raw_text and raw_text != "__START__":
            user_message = raw_text

        # ----------------------------------------------------------------
        # Emit ai:response
        # ----------------------------------------------------------------
        if is_start:
            # Use DB-stored opening messages and chip options from the Scenario table.
            # initialMessages: list of strings (character's opening chat bubbles)
            # initialChips: list of strings (tap-to-reply labels for the user)
            initial_messages = scenario_data.get("initialMessages") or []
            initial_chips_raw = scenario_data.get("initialChips") or []
            initial_chips = [
                {"id": f"opt_{chr(97 + i)}", "label": chip}
                for i, chip in enumerate(initial_chips_raw)
            ] or None

            if initial_messages:
                for i, msg_text in enumerate(initial_messages):
                    is_last = (i == len(initial_messages) - 1)
                    payload = {
                        "type": "roleplay_start",
                        "userId": user_id,
                        "roomId": room_id,
                        "messageId": message_id,
                        "sessionId": session_id,
                        "conversationId": conversation_id,
                        "replyText": msg_text,
                        "options": initial_chips if is_last else None,
                        "onboardingComplete": False,
                        "scenarioId": scenario_id,
                        "characterId": character_id,
                        "wingmanTip": None,
                        "isUserMessage": False,
                    }
                    await _emit("ai:response", payload)
                logger.info(
                    f"Roleplay start sent ({len(initial_messages)} messages) | "
                    f"userId={user_id} conv={conversation_id}"
                )
            else:
                # Fallback: no initialMessages in DB — generate opening via agent
                result = await run_roleplay_agent(
                    session_id=conversation_id or session_id,
                    user_id=user_id,
                    character_id=character_id,
                    scenario_id=scenario_id,
                    user_message=None,
                    existing_messages=existing_messages,
                    user_config=user_config,
                    character_data=character_data,
                    scenario_data=scenario_data,
                    config_fetched=True,
                    session_summaries=session_summaries,
                )
                payload = {
                    "type": "roleplay_start",
                    "userId": user_id,
                    "roomId": room_id,
                    "messageId": message_id,
                    "sessionId": session_id,
                    "conversationId": conversation_id,
                    "replyText": result["response"],
                    "options": None,
                    "onboardingComplete": False,
                    "scenarioId": scenario_id,
                    "characterId": character_id,
                    "wingmanTip": result.get("wingman_tip"),
                    "isUserMessage": False,
                }
                await _emit("ai:response", payload)
                logger.info(
                    f"Roleplay start sent (fallback agent) | "
                    f"userId={user_id} conv={conversation_id}"
                )

        else:
            # Regular subsequent roleplay turn — run agent and emit single response
            result = await run_roleplay_agent(
                session_id=conversation_id or session_id,
                user_id=user_id,
                character_id=character_id,
                scenario_id=scenario_id,
                user_message=user_message,
                existing_messages=existing_messages,
                user_config=user_config,
                character_data=character_data,
                scenario_data=scenario_data,
                config_fetched=True,
                session_summaries=session_summaries,
            )
            payload = {
                "type": "roleplay",
                "userId": user_id,
                "roomId": room_id,
                "messageId": message_id,
                "sessionId": session_id,
                "conversationId": conversation_id,
                "replyText": result["response"],
                "options": None,
                "onboardingComplete": False,
                "scenarioId": scenario_id,
                "characterId": character_id,
                "wingmanTip": result.get("wingman_tip"),
                "isUserMessage": False,
            }
            await _emit("ai:response", payload)
            logger.info(
                f"Roleplay response sent | userId={user_id} conv={conversation_id}"
            )

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(
            f"handle_roleplay error for userId={user_id}: {e}", exc_info=True
        )


# ---------------------------------------------------------------------------
# User disconnect handler
# ---------------------------------------------------------------------------

async def handle_user_disconnected(data: dict) -> None:
    """
    Handle a user:session { event: 'disconnected' } event.

    Skips summarisation if the user was in an incomplete onboarding session.
    Otherwise, fetches the active roleplay conversation from Redis and
    fires a background session summarisation task.
    """
    from services.session_summarizer import summarize_and_store_session

    user_id = data.get("userId", "")
    if not user_id:
        logger.warning("user:session disconnect missing userId — skipping")
        return

    logger.info(f"Handling disconnect for userId={user_id}")

    try:
        # ----------------------------------------------------------------
        # Skip if user is still mid-onboarding
        # ----------------------------------------------------------------
        raw_onboarding = await redis_client.get_onboarding_state_raw(user_id)
        if raw_onboarding:
            try:
                onboarding_state = json.loads(raw_onboarding)
                current_step = onboarding_state.get("current_step", "init")
                if current_step != "complete":
                    logger.info(
                        f"userId={user_id} was in onboarding (step={current_step}) "
                        "— skipping summarisation"
                    )
                    return
            except (json.JSONDecodeError, TypeError):
                pass

        # ----------------------------------------------------------------
        # Look up active roleplay session stored on roleplay_start
        # ----------------------------------------------------------------
        raw_session = await redis_client.get(
            _ACTIVE_SESSION_KEY.format(user_id=user_id)
        )
        if not raw_session:
            logger.info(
                f"No active roleplay session found for userId={user_id} "
                "— skipping summarisation"
            )
            return

        try:
            session_data = json.loads(raw_session)
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"Could not parse active session data for userId={user_id}")
            return

        conversation_id = session_data.get("conversation_id", "")
        session_id = session_data.get("session_id", "")
        character_id = session_data.get("character_id", "")
        scenario_id = session_data.get("scenario_id", "")

        if not conversation_id:
            logger.warning(
                f"Active session for userId={user_id} has no conversation_id — skipping"
            )
            return

        logger.info(
            f"Queueing session summarisation | userId={user_id} conv={conversation_id}"
        )
        await create_background_task(
            user_id=user_id,
            coro=summarize_and_store_session(
                user_id=user_id,
                conversation_id=conversation_id,
                session_id=session_id,
                character_id=character_id,
                scenario_id=scenario_id,
            ),
            task_name=f"summarize-{user_id}-{conversation_id[:12]}",
        )

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(
            f"handle_user_disconnected error for userId={user_id}: {e}", exc_info=True
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def start_socket_worker() -> None:
    """
    Connect to the socket-server as an ai_worker and wait for events.

    This coroutine blocks until the connection is permanently closed or the
    task is cancelled.  Should be launched as an asyncio background task on
    application startup.
    """
    if not settings.SOCKET_SERVER_URL:
        logger.error(
            "SOCKET_SERVER_URL is not set — socket worker will not start. "
            "Set this env var to enable real-time AI processing."
        )
        return

    if not settings.AI_WORKER_SECRET:
        logger.warning(
            "AI_WORKER_SECRET is not set — connection to socket-server may be rejected."
        )

    logger.info(f"Connecting AI worker to {settings.SOCKET_SERVER_URL} ...")
    try:
        await sio.connect(
            settings.SOCKET_SERVER_URL,
            auth={
                "role": "ai_worker",
                "secret": settings.AI_WORKER_SECRET,
            },
            transports=["websocket"],
        )
        logger.info("AI worker socket connection established — waiting for events")
        await sio.wait()
    except asyncio.CancelledError:
        logger.info("Socket worker task cancelled — disconnecting")
        if sio.connected:
            await sio.disconnect()
        raise
    except Exception as e:
        logger.error(f"Socket worker fatal error: {e}", exc_info=True)
        if sio.connected:
            await sio.disconnect()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _emit(event: str, payload: dict) -> None:
    """Emit a socket event, logging any errors without raising."""
    try:
        await sio.emit(event, payload)
    except Exception as e:
        logger.error(f"Failed to emit '{event}': {e}", exc_info=True)


async def _safe_fetch_user(user_id: str) -> dict:
    default = {"user_id": user_id, "name": "Friend", "gender": "unknown", "age": 25}
    if not user_id:
        return default
    try:
        result = await pg_client.fetch_user(user_id)
        return result or default
    except Exception as e:
        logger.warning(f"_safe_fetch_user({user_id}) failed: {e}")
        return default


async def _safe_fetch_character(character_id: str) -> dict:
    if not character_id:
        return {}
    try:
        result = await pg_client.fetch_character(character_id)
        return result or {"char_id": character_id}
    except Exception as e:
        logger.warning(f"_safe_fetch_character({character_id}) failed: {e}")
        return {"char_id": character_id}


async def _safe_fetch_scenario(scenario_id: str) -> dict:
    if not scenario_id:
        return {}
    try:
        result = await pg_client.fetch_scenario(scenario_id)
        return result or {"scenario_id": scenario_id}
    except Exception as e:
        logger.warning(f"_safe_fetch_scenario({scenario_id}) failed: {e}")
        return {"scenario_id": scenario_id}
