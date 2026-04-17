"""
Session summarization service.
Handles automatic summarization of user roleplay sessions when they disconnect.

In the Socket.IO integration, conversations are identified by conversationId
(a CUID managed by the socket-server). Messages and summaries are accessed
directly via the Postgres client.
"""
import logging
from typing import Optional
from langchain_core.messages import SystemMessage, HumanMessage

from config.pg_client import pg_client
from utils.llm import get_llm

logger = logging.getLogger(__name__)


SUMMARIZATION_PROMPT = """You are a session summarizer for a dating practice roleplay app.
Your task is to create a concise summary of the roleplay conversation session.

Focus on:
1. The scenario being practiced (what social situation the user was working on)
2. How the user engaged — their tone, approach, and any strong or weak moves
3. Key coaching moments (anything the Wingman highlighted)
4. Overall progression — did the conversation go well or stall?

Keep the summary between 300-400 characters. Be clear, concise, and informative."""


async def summarize_session(conversation_id: str) -> Optional[str]:
    """
    Summarize all messages in a conversation using LLM.

    Args:
        conversation_id: The Conversation CUID from the socket-server.

    Returns:
        Summary text (300-400 characters) or None if failed.
    """
    try:
        messages = await pg_client.fetch_messages_by_conversation(conversation_id)

        if not messages:
            logger.warning(
                f"No messages found for conversation {conversation_id[:20]}... "
                "— skipping summarisation"
            )
            return None

        logger.info(
            f"Summarising {len(messages)} messages for conversation "
            f"{conversation_id[:20]}..."
        )

        conversation_lines = []
        for msg in messages:
            sender_type = msg.get("senderType", "USER")
            text = msg.get("text", "").strip()
            if not text:
                continue
            if sender_type == "USER":
                conversation_lines.append(f"User: {text}")
            else:
                conversation_lines.append(f"Character: {text}")

        if not conversation_lines:
            logger.warning(
                f"No non-empty messages for conversation {conversation_id[:20]}..."
            )
            return None

        conversation_text = "\n".join(conversation_lines)

        llm = get_llm(temperature=0.3, streaming=False)
        llm_messages = [
            SystemMessage(content=SUMMARIZATION_PROMPT),
            HumanMessage(
                content=f"Summarize this roleplay conversation:\n\n{conversation_text}"
            ),
        ]

        response = await llm.ainvoke(llm_messages)
        summary = response.content.strip()

        if len(summary) > 400:
            logger.warning(
                f"Summary too long ({len(summary)} chars), trimming to 400"
            )
            summary = summary[:397] + "..."

        logger.info(f"Session summary created: {len(summary)} characters")
        return summary

    except Exception as e:
        logger.error(
            f"Error summarising conversation {conversation_id}: {e}", exc_info=True
        )
        return None


async def summarize_and_store_session(
    user_id: str,
    conversation_id: str,
    session_id: str = "",
    character_id: Optional[str] = None,
    scenario_id: str = "",
) -> None:
    """
    Summarise a roleplay conversation and store the result in the DB.

    Called when a user disconnects after a roleplay session.

    Args:
        user_id: The user's CUID.
        conversation_id: The Conversation CUID managed by the socket-server.
        session_id: The upstream roleplay session identifier.
        character_id: The selected character id, if already known.
        scenario_id: The selected scenario id, if already known.
    """
    try:
        logger.info(
            f"Starting session summarisation | userId={user_id} "
            f"conv={conversation_id[:20]}..."
        )

        summary = await summarize_session(conversation_id)
        if not summary:
            logger.warning(
                f"No summary generated for conv={conversation_id[:20]}... — aborting store"
            )
            return

        if not character_id:
            # Fall back to the first non-user message if Redis session data is missing.
            try:
                messages = await pg_client.fetch_messages_by_conversation(conversation_id)
                for msg in messages:
                    if msg.get("senderType") not in ("USER",):
                        character_id = msg.get("senderId")
                        break
            except Exception as e:
                logger.warning(f"Could not determine characterId for summary: {e}")

        success = await pg_client.create_session_summary(
            user_id=user_id,
            conversation_id=conversation_id,
            session_id=session_id,
            summary_text=summary,
            character_id=character_id,
            scenario_id=scenario_id,
        )

        if success:
            logger.info(
                f"Session summary stored | userId={user_id} conv={conversation_id[:20]}..."
            )
        else:
            logger.error(
                f"Failed to store session summary | userId={user_id} "
                f"conv={conversation_id[:20]}..."
            )

    except Exception as e:
        logger.error(
            f"summarize_and_store_session error | userId={user_id}: {e}", exc_info=True
        )
