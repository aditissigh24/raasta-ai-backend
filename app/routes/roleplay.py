"""
Roleplay chat API — HTTP endpoint for testing the LLM roleplay flow via Postman
or any HTTP client, bypassing the Redis pub/sub pipeline.
"""
import asyncio
import json
import logging
from typing import AsyncGenerator, List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage, AIMessage

from roleplay_agent.agent import run_roleplay_agent
from roleplay_agent.nodes.roleplay import stream_character_reply, _generate_wingman_tip
from roleplay_agent.nodes.fetch_configuration import (
    _fetch_user_config,
    _fetch_character,
    _fetch_scenario,
)
from config.prompts.roleplay_prompt import character_initiates, get_character_opening
from services.backend_client import backend_client
from utils.llm import get_llm

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/roleplay", tags=["roleplay"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ConversationMessage(BaseModel):
    """A single message in the conversation history."""
    role: str = Field(
        ...,
        description='Either "user" (what the human sent) or "ai" (what the character replied)',
        examples=["user", "ai"],
    )
    text: str = Field(..., description="Message content")


class RoleplayChatRequest(BaseModel):
    """Request body for the /chat endpoint."""

    characterId: str = Field(
        ...,
        description='Character identifier, e.g. "C01" for Riya',
        examples=["C01"],
    )
    scenarioId: str = Field(
        ...,
        description='Scenario identifier, e.g. "S01"',
        examples=["S01"],
    )
    userId: str = Field(
        ...,
        description="Caller's user ID (used to fetch their profile from backend)",
        examples=["user-abc-123"],
    )
    message: Optional[str] = Field(
        None,
        description=(
            "The message the user is sending. "
            "Leave empty / null to request the character's opening line "
            "(only valid for character-initiates scenarios)."
        ),
        examples=["Hey, loved your poha story! Indori poha is unmatched ngl"],
    )
    sessionId: Optional[str] = Field(
        None,
        description="Session ID for this conversation (pass the same value across turns to maintain history)",
        examples=["550e8400-e29b-41d4-a716-446655440000"],
    )
    history: Optional[List[ConversationMessage]] = Field(
        default=None,
        description=(
            "Previous conversation turns. "
            "Pass all prior exchanges in order so the character has memory. "
            "Each item needs 'role' (user|ai) and 'text'."
        ),
    )

    # Optional overrides — useful when backend character/scenario APIs are not yet live
    characterData: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Override: pass the full character dict directly instead of fetching from backend. "
            "Useful for local testing before backend APIs are ready."
        ),
    )
    scenarioData: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Override: pass the full scenario dict directly instead of fetching from backend."
        ),
    )


class RoleplayChatResponse(BaseModel):
    """Response from the /chat endpoint."""

    characterId: str
    scenarioId: str
    sessionId: Optional[str]
    replyText: str = Field(..., description="The character's reply")
    wingmanTip: Optional[str] = Field(
        None,
        description="Wingman coaching tip for the user (may be null if not applicable)",
    )
    characterName: Optional[str] = Field(
        None, description="Display name of the character"
    )
    isFirstMessage: bool = Field(
        False, description="True when this is the opening message of the scenario"
    )


class CharacterInfoResponse(BaseModel):
    """Character data returned by the /characters/{id} endpoint."""

    characterId: str
    data: Dict[str, Any]


class ScenarioInfoResponse(BaseModel):
    """Scenario data returned by the /scenarios/{id} endpoint."""

    scenarioId: str
    data: Dict[str, Any]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post(
    "/chat",
    response_model=RoleplayChatResponse,
    summary="Send a message to a roleplay character",
    description="""
Send a message to a roleplay character and receive:
- **replyText** — the character's in-character response
- **wingmanTip** — a Wingman coaching tip evaluating the user's move

### Multi-turn usage
Pass the full `history` array on each turn so the character remembers earlier messages.

### Character-initiates scenarios
For scenarios S03, S06, S15 the character sends the first message.
Call this endpoint with `message` set to `null` / omitted on the first turn —
the character's opening line is returned without any LLM call (fast, deterministic).

### Bypassing backend APIs (local testing)
If the backend character/scenario endpoints are not yet live, pass `characterData`
and `scenarioData` directly in the request body to skip the backend fetch.
""",
)
async def roleplay_chat(body: RoleplayChatRequest) -> RoleplayChatResponse:
    character_id = body.characterId
    scenario_id = body.scenarioId
    user_id = body.userId
    message = body.message
    session_id = body.sessionId

    # ------------------------------------------------------------------
    # Character-initiates: first turn with no message
    # ------------------------------------------------------------------
    is_first = not body.history and not message

    if is_first and character_initiates(scenario_id):
        char_data = body.characterData or {}
        if not char_data:
            try:
                char_data = await backend_client.fetch_character(character_id)
            except Exception as e:
                logger.warning(f"Could not fetch character {character_id}: {e}")

        opening = get_character_opening(scenario_id, char_data)
        return RoleplayChatResponse(
            characterId=character_id,
            scenarioId=scenario_id,
            sessionId=session_id,
            replyText=opening,
            wingmanTip=None,
            characterName=char_data.get("name"),
            isFirstMessage=True,
        )

    # ------------------------------------------------------------------
    # Validate that we have a message for non-initiating scenarios
    # ------------------------------------------------------------------
    if not message:
        raise HTTPException(
            status_code=422,
            detail=(
                f"'message' is required for scenario {scenario_id}. "
                "Only character-initiates scenarios (S03, S06, S15) can omit it."
            ),
        )

    # ------------------------------------------------------------------
    # Convert history to LangChain messages
    # ------------------------------------------------------------------
    existing_messages = []
    for turn in body.history or []:
        if turn.role == "user":
            existing_messages.append(HumanMessage(content=turn.text))
        else:
            existing_messages.append(AIMessage(content=turn.text))

    # ------------------------------------------------------------------
    # Fetch user config (best-effort)
    # ------------------------------------------------------------------
    user_config: dict = {"user_id": user_id, "name": "Friend", "gender": "unknown", "age": 25}
    try:
        fetched = await backend_client.fetch_user_config(user_id)
        user_config = dict(fetched)
    except Exception as e:
        logger.warning(f"Could not fetch user config for {user_id}: {e}. Using defaults.")

    # ------------------------------------------------------------------
    # Resolve character & scenario data
    # ------------------------------------------------------------------
    character_data = body.characterData
    scenario_data = body.scenarioData
    config_fetched = bool(character_data and scenario_data)

    # If either override is missing, let the agent fetch from backend
    if not config_fetched:
        character_data = None
        scenario_data = None

    # ------------------------------------------------------------------
    # Run the agent
    # ------------------------------------------------------------------
    try:
        result = await run_roleplay_agent(
            session_id=session_id or "",
            user_id=user_id,
            character_id=character_id,
            scenario_id=scenario_id,
            user_message=message,
            existing_messages=existing_messages,
            user_config=user_config,
            character_data=character_data or {},
            scenario_data=scenario_data or {},
            config_fetched=config_fetched,
        )
    except Exception as e:
        logger.error(f"Roleplay agent error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Agent error: {e}")

    char_name = result.get("character_data", {}).get("name")

    return RoleplayChatResponse(
        characterId=character_id,
        scenarioId=scenario_id,
        sessionId=session_id,
        replyText=result["response"],
        wingmanTip=result.get("wingman_tip"),
        characterName=char_name,
        isFirstMessage=is_first,
    )


@router.post(
    "/chat/stream",
    summary="Send a message to a roleplay character (streaming)",
    description="""
Stream the character's reply token-by-token using Server-Sent Events (SSE).

Accepts the same request body as `/chat`.

### Event stream format
Each event is a JSON object on a `data:` line followed by two newlines.

- **token** — one content chunk from the LLM:
  `data: {"type": "token", "content": "Hey there"}`
- **done** — final event after the stream completes, contains metadata:
  `data: {"type": "done", "wingmanTip": "...", "characterName": "Riya", "isFirstMessage": false, "sessionId": "..."}`
- **error** — sent if something goes wrong:
  `data: {"type": "error", "message": "..."}`
""",
)
async def roleplay_chat_stream(body: RoleplayChatRequest) -> StreamingResponse:
    character_id = body.characterId
    scenario_id = body.scenarioId
    user_id = body.userId
    message = body.message
    session_id = body.sessionId
    is_first = not body.history and not message

    async def event_generator() -> AsyncGenerator[str, None]:
        def sse(payload: dict) -> str:
            return f"data: {json.dumps(payload)}\n\n"

        try:
            # ------------------------------------------------------------------
            # Character-initiates: first turn with no message — fast path
            # ------------------------------------------------------------------
            if is_first and character_initiates(scenario_id):
                char_data = body.characterData or {}
                if not char_data:
                    char_data = await _fetch_character(character_id)

                opening = get_character_opening(scenario_id, char_data)
                yield sse({
                    "type": "done",
                    "replyText": opening,
                    "wingmanTip": None,
                    "characterName": char_data.get("name"),
                    "isFirstMessage": True,
                    "sessionId": session_id,
                    "characterId": character_id,
                    "scenarioId": scenario_id,
                })
                return

            # ------------------------------------------------------------------
            # Validate that we have a message for non-initiating scenarios
            # ------------------------------------------------------------------
            if not message:
                yield sse({
                    "type": "error",
                    "message": (
                        f"'message' is required for scenario {scenario_id}. "
                        "Only character-initiates scenarios (S03, S06, S15) can omit it."
                    ),
                })
                return

            # ------------------------------------------------------------------
            # Convert history to LangChain messages
            # ------------------------------------------------------------------
            existing_messages = []
            for turn in body.history or []:
                if turn.role == "user":
                    existing_messages.append(HumanMessage(content=turn.text))
                else:
                    existing_messages.append(AIMessage(content=turn.text))
            existing_messages.append(HumanMessage(content=message))

            # ------------------------------------------------------------------
            # Fetch config in parallel (or use overrides from body)
            # ------------------------------------------------------------------
            if body.characterData and body.scenarioData:
                character_data = body.characterData
                scenario_data = body.scenarioData
                user_config = {"user_id": user_id, "name": "Friend", "gender": "unknown", "age": 25}
                try:
                    user_config = await _fetch_user_config(user_id)
                except Exception as e:
                    logger.warning(f"Could not fetch user config for {user_id}: {e}")
            else:
                user_config, character_data, scenario_data = await asyncio.gather(
                    _fetch_user_config(user_id),
                    _fetch_character(character_id),
                    _fetch_scenario(scenario_id),
                )

            char_name = character_data.get("name")

            # ------------------------------------------------------------------
            # Stream the character reply
            # ------------------------------------------------------------------
            collected_reply_parts: list[str] = []
            async for token in stream_character_reply(
                character_data=character_data,
                scenario_data=scenario_data,
                user_config=user_config,
                messages=existing_messages,
                scenario_id=scenario_id,
                session_summaries=[],
            ):
                collected_reply_parts.append(token)
                yield sse({"type": "token", "content": token})

            full_reply = "".join(collected_reply_parts)

            # ------------------------------------------------------------------
            # Generate wingman tip (non-streaming, after reply is complete)
            # ------------------------------------------------------------------
            llm = get_llm()
            wingman_tip = await _generate_wingman_tip(
                llm=llm,
                scenario_data=scenario_data,
                user_message=message,
                character_reply=full_reply,
                char_data=character_data,
            )

            # ------------------------------------------------------------------
            # Final done event
            # ------------------------------------------------------------------
            yield sse({
                "type": "done",
                "replyText": full_reply,
                "wingmanTip": wingman_tip,
                "characterName": char_name,
                "isFirstMessage": is_first,
                "sessionId": session_id,
                "characterId": character_id,
                "scenarioId": scenario_id,
            })

        except Exception as e:
            logger.error(f"Streaming roleplay error: {e}", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get(
    "/characters/{character_id}",
    response_model=CharacterInfoResponse,
    summary="Fetch character data",
    description="Retrieve a character record from the backend. Useful for inspecting what the LLM receives.",
)
async def get_character(character_id: str) -> CharacterInfoResponse:
    try:
        data = await backend_client.fetch_character(character_id)
        return CharacterInfoResponse(characterId=character_id, data=data)
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Could not fetch character {character_id} from backend: {e}",
        )


@router.get(
    "/scenarios/{scenario_id}",
    response_model=ScenarioInfoResponse,
    summary="Fetch scenario data",
    description="Retrieve a scenario record from the backend. Useful for inspecting what the LLM receives.",
)
async def get_scenario(scenario_id: str) -> ScenarioInfoResponse:
    try:
        data = await backend_client.fetch_scenario(scenario_id)
        return ScenarioInfoResponse(scenarioId=scenario_id, data=data)
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Could not fetch scenario {scenario_id} from backend: {e}",
        )
