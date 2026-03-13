"""
Mixpanel client for sending server-side events.
Tracks user engagement events like Chat Started and Chat Engaged.
"""
import logging
from unittest.signals import registerResult
import httpx
from typing import Dict, Optional, Any
from config.settings import settings

logger = logging.getLogger(__name__)


class MixpanelClient:
    """Client for sending server-side events to Mixpanel."""
    
    def __init__(
        self,
        project_token: str | None = None
    ):
        """
        Initialize Mixpanel client.
        
        Args:
            project_token: Mixpanel project token. Defaults to settings.MIXPANEL_PROJECT_TOKEN
        """
        self.project_token = project_token or settings.MIXPANEL_PROJECT_TOKEN
        self.api_url = "https://api-eu.mixpanel.com/track?verbose=1"
        
        self._is_configured = bool(self.project_token)
        
        if not self._is_configured:
            logger.warning("⚠️ Mixpanel not configured. Events will be logged but not sent.")
    
    @property
    def is_configured(self) -> bool:
        """Check if Mixpanel is properly configured."""
        return self._is_configured
    
    async def send_event(
        self,
        distinct_id: str,
        event_name: str,
        properties: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Send an event to Mixpanel via HTTP API.
        
        Args:
            distinct_id: Unique identifier for the user
            event_name: Name of the event (e.g., "ChatStarted", "ChatEngaged")
            properties: Additional event properties
            
        Returns:
            True if event sent successfully, False otherwise
        """
        if not self.is_configured:
            logger.debug(f"📊 Mixpanel not configured. Would send event: {event_name} for user {distinct_id}")
            return False
        
        try:
            # Prepare event properties
            event_properties = properties or {}
            event_properties["token"] = self.project_token
            event_properties["distinct_id"] = distinct_id
            
            # Prepare payload
            payload = [{
                "event": event_name,
                "properties": event_properties
            }]
            
            # Send event to Mixpanel
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.api_url,
                    json=payload,
                    headers={
                        "accept": "application/json",
                        "content-type": "application/json"
                    },
                    timeout=10.0
                )
                response.raise_for_status()
                
                result = response.json()
                logger.info(f"Mixpanel raw response: {response.text}")
                logger.info(f"Mixpanel parsed response: {result}")
                
                # Mixpanel returns {"status": 1} for success, or just 1 as integer
                # Handle both response formats
                if isinstance(result, dict):
                    status = result.get("status", 0)
                elif isinstance(result, int):
                    status = result
                else:
                    logger.warning(f"⚠️ Unexpected Mixpanel response type: {type(result)}, value: {result}")
                    status = 0
                
                if status == 1:
                    logger.info(f"✅ Mixpanel event sent: {event_name} for user {distinct_id[:20]}...")
                    return True
                else:
                    logger.warning(f"⚠️ Mixpanel event not accepted: {event_name}, response: {result}")
                    return False
        
        except httpx.HTTPError as e:
            logger.error(f"❌ HTTP error sending Mixpanel event {event_name}: {e}")
            if hasattr(e, 'response') and e.response:
                logger.error(f"Response: {e.response.text}")
            return False
        
        except Exception as e:
            logger.error(f"❌ Error sending Mixpanel event {event_name}: {e}")
            return False
    
    async def send_chat_started_event(
        self,
        user_id: str,
        session_id: str,
        coach_type: str,
        conversation_id: str
    ) -> bool:
        """
        Send Chat Started event (fires on first message in session).
        
        Args:
            user_id: User UID
            session_id: Unique session identifier
            coach_type: Type of coach (kabir, tara, vikram)
            conversation_id: Conversation ID
            
        Returns:
            True if successful, False otherwise
        """
        properties = {
            "coach_type": coach_type,
            "conversation_id": conversation_id,
            "session_id": session_id,
            "message_count": 1,
            "source": "love-doc-ai"
        }
        
        return await self.send_event(
            distinct_id=user_id,
            event_name="ChatStarted",
            properties=properties
        )
    
    async def send_chat_engaged_event(
        self,
        user_id: str,
        session_id: str,
        coach_type: str,
        conversation_id: str
    ) -> bool:
        """
        Send Chat Engaged event (fires on third message in session).
        
        Args:
            user_id: User UID
            session_id: Unique session identifier
            coach_type: Type of coach (kabir, tara, vikram)
            conversation_id: Conversation ID
            
        Returns:
            True if successful, False otherwise
        """
        properties = {
            "coach_type": coach_type,
            "conversation_id": conversation_id,
            "session_id": session_id,
            "message_count": 3,
            "source": "love-doc-ai"
        }
        
        return await self.send_event(
            distinct_id=user_id,
            event_name="ChatEngaged",
            properties=properties
        )


# Singleton instance for convenience
mixpanel_client = MixpanelClient()
