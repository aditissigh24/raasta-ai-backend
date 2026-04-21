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

Roleplay streaming protocol (new):
    roleplay_typing  — emitted immediately on recovery path (shows "thinking...")
    roleplay_token   — batched text chunks (every 4 tokens or 30ms)
    roleplay         — final complete event after stream ends
    roleplay_error   — emitted if stream fails mid-response

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
from onboarding_agent.agent import run_onboarding_agent
from services.webhook_handler import convert_db_messages_to_langchain
from services.task_manager import create_background_task
from wingman import pipeline as wingman_pipeline

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

    roleplay_start:
        - Record active session in Redis
        - Emit DB-stored opening messages (initialMessages / initialChips)
        - Prime the Redis session cache (no-wait background task)

    roleplay (subsequent turns):
        - Stream tokens via wingman pipeline
        - Emit roleplay_token batches as they arrive
        - Emit final roleplay event after stream completes
        - Emit roleplay_error on failure
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
        # On roleplay_start, record active session in Redis and prime cache
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

            # Fetch scenario for opening messages
            scenario_data = await _safe_fetch_scenario(scenario_id)
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

                # Pre-warm Redis session cache in the background (don't block emit)
                asyncio.create_task(
                    wingman_pipeline.prime_session_cache(conversation_id),
                    name=f"prime-cache-{conversation_id[:8]}",
                )

                logger.info(
                    f"Roleplay start sent ({len(initial_messages)} messages) | "
                    f"userId={user_id} conv={conversation_id}"
                )
            else:
                # No initialMessages — generate opening via pipeline and prime cache
                await wingman_pipeline.prime_session_cache(conversation_id)

                full_response = ""
                try:
                    async for chunk in wingman_pipeline.handle_turn_streaming(
                        conversation_id=conversation_id,
                        user_message="",
                        user_id=user_id,
                    ):
                        full_response += chunk
                except Exception:
                    pass  # errors handled inside pipeline

                payload = {
                    "type": "roleplay_start",
                    "userId": user_id,
                    "roomId": room_id,
                    "messageId": message_id,
                    "sessionId": session_id,
                    "conversationId": conversation_id,
                    "replyText": full_response,
                    "options": None,
                    "onboardingComplete": False,
                    "scenarioId": scenario_id,
                    "characterId": character_id,
                    "wingmanTip": None,
                    "isUserMessage": False,
                }
                await _emit("ai:response", payload)
                logger.info(
                    f"Roleplay start sent (pipeline fallback) | "
                    f"userId={user_id} conv={conversation_id}"
                )

        else:
            # ----------------------------------------------------------------
            # Regular roleplay turn — stream tokens to client
            # ----------------------------------------------------------------
            user_message: Optional[str] = None
            if raw_text and raw_text != "__START__":
                user_message = raw_text

            if not user_message:
                logger.warning(f"handle_roleplay: empty user message for userId={user_id}")
                return

            # Emit typing indicator immediately if this will be a recovery turn.
            # The pipeline's pre-LLM phase is fast (Redis hit), but we emit
            # roleplay_typing proactively so the frontend can show something instantly.
            await _emit("ai:response", {
                "type":      "roleplay_typing",
                "userId":    user_id,
                "roomId":    room_id,
                "messageId": message_id,
            })

            full_response = ""
            _buffer = ""
            _last_emit_ts = asyncio.get_event_loop().time()
            BATCH_TOKENS = 4
            BATCH_MS = 0.030  # 30ms

            try:
                async for chunk in wingman_pipeline.handle_turn_streaming(
                    conversation_id=conversation_id,
                    user_message=user_message,
                    user_id=user_id,
                ):
                    full_response += chunk
                    _buffer += chunk
                    now = asyncio.get_event_loop().time()

                    # Batch: emit when buffer has 4+ tokens OR 30ms has passed
                    if len(_buffer) >= BATCH_TOKENS or (now - _last_emit_ts) >= BATCH_MS:
                        await _emit("ai:response", {
                            "type":      "roleplay_token",
                            "userId":    user_id,
                            "roomId":    room_id,
                            "messageId": message_id,
                            "chunk":     _buffer,
                        })
                        _buffer = ""
                        _last_emit_ts = now

                # Flush any remaining buffer
                if _buffer:
                    await _emit("ai:response", {
                        "type":      "roleplay_token",
                        "userId":    user_id,
                        "roomId":    room_id,
                        "messageId": message_id,
                        "chunk":     _buffer,
                    })

                # Final complete event
                await _emit("ai:response", {
                    "type":             "roleplay",
                    "userId":           user_id,
                    "roomId":           room_id,
                    "messageId":        message_id,
                    "sessionId":        session_id,
                    "conversationId":   conversation_id,
                    "replyText":        full_response,
                    "options":          None,
                    "onboardingComplete": False,
                    "scenarioId":       scenario_id,
                    "characterId":      character_id,
                    "wingmanTip":       None,  # sent later via wingman_tip event
                    "isUserMessage":    False,
                })
                logger.info(
                    f"Roleplay response streamed | userId={user_id} "
                    f"conv={conversation_id} tokens={len(full_response)}"
                )

            except Exception as stream_err:
                logger.error(
                    f"Stream failed for userId={user_id} conv={conversation_id}: "
                    f"{stream_err}",
                    exc_info=True,
                )
                await _emit("ai:response", {
                    "type":      "roleplay_error",
                    "userId":    user_id,
                    "roomId":    room_id,
                    "messageId": message_id,
                    "error":     "Response generation failed — please try again",
                })

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
