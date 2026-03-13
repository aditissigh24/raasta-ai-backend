"""
Session summarization service.
Handles automatic summarization of user sessions when they disconnect.
"""
import asyncio
import logging
from typing import Optional
from langchain_core.messages import SystemMessage, HumanMessage

from services.backend_client import backend_client
from utils.llm import get_llm

logger = logging.getLogger(__name__)


SUMMARIZATION_PROMPT = """You are a session summarizer for relationship coaching conversations. 
Your task is to create a concise summary of the conversation session.

Focus on:
1. Main topics discussed
2. User's situation or problem
3. Coach's key advice or insights
4. Any action items or decisions made

Keep the summary between 300-400 characters. Be clear, concise, and informative."""


async def summarize_session(session_id: str) -> Optional[str]:
    """
    Summarize all messages in a session using LLM.
    
    Args:
        session_id: The session identifier
        
    Returns:
        Summary text (300-400 characters) or None if failed
    """
    try:
        # Fetch all messages for this session
        messages = await backend_client.fetch_messages_by_session(session_id)
        
        if not messages:
            logger.warning(f"No messages found for session {session_id[:30]}...")
            return None
        
        logger.info(f"📝 Summarizing {len(messages)} messages for session {session_id[:30]}...")
        
        # Format messages as conversation transcript
        conversation_lines = []
        for msg in messages:
            sender_type = msg.get("senderType", "USER")
            text = msg.get("text", "")
            
            if sender_type == "USER":
                conversation_lines.append(f"User: {text}")
            elif sender_type == "COACH":
                conversation_lines.append(f"Coach: {text}")
        
        conversation_text = "\n".join(conversation_lines)
        
        # Create LLM prompt
        llm = get_llm(temperature=0.3, streaming=False)
        
        messages_to_llm = [
            SystemMessage(content=SUMMARIZATION_PROMPT),
            HumanMessage(content=f"Summarize this conversation:\n\n{conversation_text}")
        ]
        
        # Get summary from LLM
        response = await llm.ainvoke(messages_to_llm)
        summary = response.content.strip()
        
        # Validate and trim if necessary
        if len(summary) > 400:
            logger.warning(f"Summary too long ({len(summary)} chars), trimming to 400")
            summary = summary[:397] + "..."
        
        logger.info(f"✅ Session summary created: {len(summary)} characters")
        return summary
        
    except Exception as e:
        logger.error(f"Error summarizing session {session_id}: {e}", exc_info=True)
        return None


async def summarize_and_store_session(user_id: str, session_id: str):
    """
    Summarize a session and store it in the database.
    
    This is the main entry point called when a user disconnects.
    
    Args:
        user_id: User UID
        session_id: Session identifier
    """
    try:
        logger.info(f"🔄 Starting session summarization for user {user_id}, session {session_id[:30]}...")
        
        # Generate summary
        summary = await summarize_session(session_id)
        
        if not summary:
            logger.warning(f"Failed to generate summary for session {session_id[:30]}...")
            return
        
        # Get messages to determine which coach this session was with
        messages = await backend_client.fetch_messages_by_session(session_id)
        
        if not messages:
            logger.warning(f"No messages found to determine coach for session {session_id[:30]}...")
            return
        
        # Find coach from messages (look for COACH sender)
        coach_sender_id = None
        chatroom_id = None
        for msg in messages:
            if msg.get("senderType") == "COACH":
                coach_sender_id = msg.get("senderId")
            # Extract chatroomId from any message (all messages in session have same chatroomId)
            if not chatroom_id and msg.get("chatroomId"):
                chatroom_id = msg.get("chatroomId")
                logger.info(f"chatrrom id is : {chatroom_id}")
    
            if coach_sender_id and chatroom_id:
                break
        
        if not coach_sender_id:
            logger.warning(f"Could not determine coach for session {session_id[:30]}...")
            return
        if not chatroom_id:
            logger.warning(f"Could not determine chatroomId for session {session_id[:30]}...")
            return
        
        # Resolve coach DB ID -- senderId may already be numeric or a string key
        coach_db_id = None
        try:
            coach_db_id = int(coach_sender_id)
        except (ValueError, TypeError):
            coach = await backend_client.get_coach_by_type(coach_sender_id)
            if coach:
                coach_db_id = coach.get("id")

        if not coach_db_id:
            logger.warning(f"Could not resolve coach DB ID for {coach_sender_id}")
            return
        
        # Store summary in database
        success = await backend_client.create_session_summary(
            user_id=user_id,
            coach_id=coach_db_id,
            conversation_id= chatroom_id,
            session_id=session_id,
            summary_text=summary
        )
        
        if success:
            logger.info(f"✅ Session summary stored successfully for session {session_id[:30]}...")

            from services.genuine_check import check_conversation_genuineness
            asyncio.create_task(check_conversation_genuineness(
                session_id=session_id,
                user_id=user_id,
                conversation_id=chatroom_id,
                coach_uid=coach_sender_id,
            ))
        else:
            logger.error(f"❌ Failed to store session summary for session {session_id[:30]}...")
            
    except Exception as e:
        logger.error(f"Error in summarize_and_store_session: {e}", exc_info=True)

