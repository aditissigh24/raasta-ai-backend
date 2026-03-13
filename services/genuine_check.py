"""
Genuine conversation checker.
Uses an LLM to evaluate whether a user's session was a meaningful,
platform-relevant conversation and fires the appropriate analytics event.
"""
import asyncio
import logging
from typing import Optional, Dict, Literal, List

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from pydantic import BaseModel, Field

from config.settings import settings
from services.backend_client import backend_client
from services.meta_pixel_client import meta_pixel_client
from services.mixpanel_client import mixpanel_client
from services.webhook_handler import store_event_in_mongodb

logger = logging.getLogger(__name__)


NOT_GENUINE_KEYWORDS = [
    "TIMEPASS",
    "GIBBERISH",
    "TESTING",
    "INAPPROPRIATE",
    "PERSONAL_INFO_REQUEST",
    "GREETING_ONLY",
    "OFF_TOPIC",
    "SPAM",
]


class GenuineCheckResult(BaseModel):
    """Structured LLM output for conversation genuineness classification."""
    is_genuine: bool = Field(
        description="True if the user had a meaningful conversation relevant to the platform's purpose."
    )
    reason: str = Field(
        description="One short line explaining the classification."
    )
    not_genuine_keyword: Optional[Literal[
        "TIMEPASS", "GIBBERISH", "TESTING", "INAPPROPRIATE",
        "PERSONAL_INFO_REQUEST", "GREETING_ONLY", "OFF_TOPIC", "SPAM",
    ]] = Field(
        None,
        description=(
            "Required when is_genuine is False. "
            "The keyword category for why the conversation was not genuine."
        ),
    )


GENUINENESS_PROMPT = """\
You are an analyst for a relationship-coaching platform.
Your job is to decide whether a user's conversation with their coach was
**genuinely meaningful** or not.

A conversation is GENUINE when the user:
- Discusses a real relationship issue, breakup, dating problem, or emotional struggle
- Asks for advice on communication, conflict resolution, self-improvement, or personal growth
- Shares feelings, context, or details about their situation
- Engages back-and-forth with the coach in a substantive way

A conversation is NOT GENUINE when the user is:
- Just doing timepass with no real intent (keyword: TIMEPASS)
- Sending gibberish, random text, or keyboard mashing (keyword: GIBBERISH)
- Testing the bot or system capabilities (keyword: TESTING)
- Sending sexual, abusive, or wildly off-topic inappropriate content (keyword: INAPPROPRIATE)
- Asking for the coach's personal details, photos, or social media (keyword: PERSONAL_INFO_REQUEST)
- Only saying hi/hello with no substantive follow-up (keyword: GREETING_ONLY)
- Discussing topics completely unrelated to relationships or personal growth (keyword: OFF_TOPIC)
- Sending repetitive or promotional content (keyword: SPAM)

Rules:
1. Set `is_genuine` to true or false.
2. Always provide a concise one-line `reason`.
3. If `is_genuine` is false you MUST set `not_genuine_keyword` to exactly one of the
   keywords listed above. If `is_genuine` is true, leave `not_genuine_keyword` as null.
"""


def _format_conversation(messages: List[Dict]) -> str:
    lines: list[str] = []
    for msg in messages:
        sender_type = msg.get("senderType", "USER")
        text = msg.get("text", "")
        if sender_type == "USER":
            lines.append(f"User: {text}")
        else:
            lines.append(f"Coach: {text}")
    return "\n".join(lines)


def _build_user_info_block(user_config: Dict) -> str:
    name = user_config.get("name", "Unknown")
    gender = user_config.get("gender", "unknown")
    age = user_config.get("ageRange", user_config.get("age", "unknown"))
    situation = user_config.get("currentSituation", "")
    parts = [
        f"Name: {name}",
        f"Gender: {gender}",
        f"Age: {age}",
    ]
    if situation:
        parts.append(f"Current situation: {situation}")
    return "\n".join(parts)


def _prepare_meta_user_data(user_config: Dict) -> Dict:
    """Extract and normalise user fields for Meta Pixel enrichment."""
    phone = user_config.get("phone", "")
    country_code = user_config.get("countryCode", "")
    if phone and country_code and phone.startswith(country_code.replace("+", "")):
        phone = phone[len(country_code.replace("+", "")):]

    full_name = user_config.get("name", "")
    name_parts = full_name.split(" ", 1) if full_name else []
    first_name = name_parts[0] if name_parts else ""
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    gender = user_config.get("gender", "")
    if gender:
        gl = gender.lower()
        if "male" in gl and "female" not in gl:
            gender = "m"
        elif "female" in gl:
            gender = "f"
        else:
            gender = ""

    data = {
        "email": user_config.get("email", ""),
        "phone": phone,
        "first_name": first_name,
        "last_name": last_name,
        "gender": gender,
        "country": "in",
    }
    return {k: v for k, v in data.items() if v}


async def _fire_genuine_conversation_event(
    user_id: str,
    session_id: str,
    coach_type: str,
    conversation_id: str,
    coach_uid: str,
    reason: str,
    message_count: int,
    user_data_dict: Dict,
) -> None:
    """Send Genuine_conversation event to Meta, Mixpanel, and MongoDB."""

    event_props = {
        "user_id": user_id,
        "session_id": session_id,
        "coach_type": coach_type,
        "conversation_id": conversation_id,
        "reason": reason,
        "message_count": message_count,
        "source": "love-doc-ai",
    }

    logger.info(f"Firing Genuine_conversation for user {user_id}, session {session_id[:30]}...")

    asyncio.create_task(
        meta_pixel_client.send_server_event(
            event_name="Genuine_conversation",
            user_id=user_id,
            session_id=session_id,
            custom_data={
                **event_props,
                "value": 1.0,
                "currency": "INR",
            },
            email=user_data_dict.get("email"),
            phone=user_data_dict.get("phone"),
            first_name=user_data_dict.get("first_name"),
            last_name=user_data_dict.get("last_name"),
            gender=user_data_dict.get("gender"),
            country=user_data_dict.get("country"),
        )
    )

    asyncio.create_task(
        mixpanel_client.send_event(
            distinct_id=user_id,
            event_name="Genuine_conversation",
            properties=event_props,
        )
    )

    asyncio.create_task(
        store_event_in_mongodb(
            event_name="GENUINE_CONVERSATION",
            collection_name="coach_events",
            event_properties={
                **event_props,
                "coach_uid": coach_uid,
            },
            distinct_id=user_id,
        )
    )


async def _fire_not_genuine_conversation_event(
    user_id: str,
    session_id: str,
    coach_type: str,
    conversation_id: str,
    coach_uid: str,
    reason: str,
    not_genuine_keyword: str,
    message_count: int,
    user_data_dict: Dict,
) -> None:
    """Send Not_genuine_conversation event to Meta, Mixpanel, and MongoDB."""

    event_props = {
        "user_id": user_id,
        "session_id": session_id,
        "coach_type": coach_type,
        "conversation_id": conversation_id,
        "reason": reason,
        "not_genuine_keyword": not_genuine_keyword,
        "message_count": message_count,
        "source": "love-doc-ai",
    }

    logger.info(
        f"Firing Not_genuine_conversation ({not_genuine_keyword}) "
        f"for user {user_id}, session {session_id[:30]}..."
    )

    asyncio.create_task(
        meta_pixel_client.send_server_event(
            event_name="Not_genuine_conversation",
            user_id=user_id,
            session_id=session_id,
            custom_data={
                **event_props,
                "value": 0.0,
                "currency": "INR",
            },
            email=user_data_dict.get("email"),
            phone=user_data_dict.get("phone"),
            first_name=user_data_dict.get("first_name"),
            last_name=user_data_dict.get("last_name"),
            gender=user_data_dict.get("gender"),
            country=user_data_dict.get("country"),
        )
    )

    asyncio.create_task(
        mixpanel_client.send_event(
            distinct_id=user_id,
            event_name="Not_genuine_conversation",
            properties=event_props,
        )
    )

    asyncio.create_task(
        store_event_in_mongodb(
            event_name="NOT_GENUINE_CONVERSATION",
            collection_name="coach_events",
            event_properties={
                **event_props,
                "coach_uid": coach_uid,
            },
            distinct_id=user_id,
        )
    )


async def _resolve_coach_type(coach_uid: str) -> str:
    """Resolve a human-readable coach type from a coach identifier."""
    resolved = await backend_client.resolve_coach_type(coach_uid)
    return resolved or coach_uid


async def check_conversation_genuineness(
    session_id: str,
    user_id: str,
    conversation_id: str,
    coach_uid: str,
    coach_type: Optional[str] = None,
) -> Optional[GenuineCheckResult]:
    """
    Evaluate whether the session's conversation was genuinely meaningful
    and fire the appropriate analytics event.

    Returns the LLM result for testing/logging, or None on failure.
    """
    try:
        messages = await backend_client.fetch_messages_by_session(session_id)
        if not messages:
            logger.warning(f"No messages for session {session_id[:30]}... — skipping genuine check")
            return None

        if not coach_type:
            coach_type = await _resolve_coach_type(coach_uid)

        user_config = await backend_client.fetch_user_config(user_id)

        conversation_text = _format_conversation(messages)
        user_info_text = _build_user_info_block(user_config)
        message_count = len(messages)

        llm = ChatOpenAI(
            model=settings.OPENAI_MODEL,
            temperature=0,
            api_key=settings.OPENAI_API_KEY,
        )
        structured_llm = llm.with_structured_output(GenuineCheckResult)

        llm_messages = [
            SystemMessage(content=GENUINENESS_PROMPT),
            HumanMessage(content=(
                f"## User Info\n{user_info_text}\n\n"
                f"## Conversation ({message_count} messages)\n{conversation_text}"
            )),
        ]

        result: GenuineCheckResult = await structured_llm.ainvoke(llm_messages)

        logger.info(
            f"Genuine check for session {session_id[:30]}...: "
            f"is_genuine={result.is_genuine}, reason={result.reason}"
        )

        user_data_dict = _prepare_meta_user_data(user_config)

        if result.is_genuine:
            await _fire_genuine_conversation_event(
                user_id=user_id,
                session_id=session_id,
                coach_type=coach_type,
                conversation_id=conversation_id,
                coach_uid=coach_uid,
                reason=result.reason,
                message_count=message_count,
                user_data_dict=user_data_dict,
            )
        else:
            keyword = result.not_genuine_keyword or "TIMEPASS"
            await _fire_not_genuine_conversation_event(
                user_id=user_id,
                session_id=session_id,
                coach_type=coach_type,
                conversation_id=conversation_id,
                coach_uid=coach_uid,
                reason=result.reason,
                not_genuine_keyword=keyword,
                message_count=message_count,
                user_data_dict=user_data_dict,
            )

        return result

    except Exception as e:
        logger.error(f"Error in genuine check for session {session_id}: {e}", exc_info=True)
        return None
