"""
Meta Pixel client for sending server-side events via Conversions API.
Tracks user engagement events like Chat Started and Chat Engaged.
"""
import httpx
import hashlib
import logging
import time
from typing import Dict, Optional, Any
from config.settings import settings

logger = logging.getLogger(__name__)


class MetaPixelClient:
    """Client for sending server-side events to Meta Pixel Conversions API."""
    
    def __init__(
        self,
        pixel_id: str | None = None,
        access_token: str | None = None,
        test_event_code: str | None = None
    ):
        """
        Initialize Meta Pixel client.
        
        Args:
            pixel_id: Meta Pixel ID. Defaults to settings.META_PIXEL_ID
            access_token: Access token for Conversions API. Defaults to settings.META_PIXEL_ACCESS_TOKEN
            test_event_code: Optional test event code for testing in Events Manager
        """
        self.pixel_id = pixel_id or settings.META_PIXEL_ID
        self.access_token = access_token or settings.META_PIXEL_ACCESS_TOKEN
        self.test_event_code = test_event_code or settings.META_PIXEL_TEST_EVENT_CODE
        
        # Meta Conversions API endpoint (v19.0+)
        self.api_version = "v19.0"
        self.base_url = f"https://graph.facebook.com/{self.api_version}"
        
        self._is_configured = bool(self.pixel_id and self.access_token)
        
        if not self._is_configured:
            logger.warning("⚠️ Meta Pixel not configured. Events will be logged but not sent.")
    
    @property
    def is_configured(self) -> bool:
        """Check if Meta Pixel is properly configured."""
        return self._is_configured
    
    @staticmethod
    def hash_user_data(value: str) -> str:
        """
        Hash user data using SHA-256 for privacy.
        
        Args:
            value: Value to hash (e.g., user ID, email)
            
        Returns:
            SHA-256 hash of the value in lowercase hex format
        """
        if not value:
            return ""
        
        # Normalize: trim whitespace and convert to lowercase
        normalized = value.strip().lower()
        
        # Hash using SHA-256
        return hashlib.sha256(normalized.encode('utf-8')).hexdigest()
    
    async def send_server_event(
        self,
        event_name: str,
        user_id: str,
        session_id: str,
        custom_data: Optional[Dict[str, Any]] = None,
        client_ip: Optional[str] = None,
        client_user_agent: Optional[str] = None,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        gender: Optional[str] = None,
        date_of_birth: Optional[str] = None,
        city: Optional[str] = None,
        state: Optional[str] = None,
        zip_code: Optional[str] = None,
        country: Optional[str] = None
    ) -> bool:
        """
        Send a server event to Meta Pixel Conversions API.
        
        Args:
            event_name: Name of the event (e.g., "ChatStarted", "ChatEngaged")
            user_id: User UID
            session_id: Unique session identifier
            custom_data: Additional custom parameters
            client_ip: Client IP address (optional)
            client_user_agent: Client user agent (optional)
            email: User email (optional, will be hashed)
            phone: User phone number (optional, will be hashed)
            first_name: User first name (optional, will be hashed)
            last_name: User last name (optional, will be hashed)
            gender: User gender (optional, will be hashed)
            date_of_birth: User date of birth in YYYYMMDD format (optional, will be hashed)
            city: User city (optional, will be hashed)
            state: User state (optional, will be hashed)
            zip_code: User zip code (optional, will be hashed)
            country: User country code (optional, will be hashed)
            
        Returns:
            True if event sent successfully, False otherwise
        """
        if not self.is_configured:
            logger.debug(f"📊 Meta Pixel not configured. Would send event: {event_name} for user {user_id}")
            return False
        
        try:
            # Prepare user data (hashed for privacy)
            user_data = {
                "external_id": self.hash_user_data(user_id)
            }
            
            # Add optional hashed user data if available
            if email:
                user_data["em"] = self.hash_user_data(email)
            if phone:
                user_data["ph"] = self.hash_user_data(phone)
            if first_name:
                user_data["fn"] = self.hash_user_data(first_name)
            if last_name:
                user_data["ln"] = self.hash_user_data(last_name)
            if gender:
                user_data["ge"] = self.hash_user_data(gender)
            if date_of_birth:
                user_data["db"] = self.hash_user_data(date_of_birth)
            if city:
                user_data["ct"] = self.hash_user_data(city)
            if state:
                user_data["st"] = self.hash_user_data(state)
            if zip_code:
                user_data["zp"] = self.hash_user_data(zip_code)
            if country:
                user_data["country"] = self.hash_user_data(country)
            
            # Add optional non-hashed user data if available
            if client_ip:
                user_data["client_ip_address"] = client_ip
            if client_user_agent:
                user_data["client_user_agent"] = client_user_agent
            
            # Prepare event data
            event_data = {
                "event_name": event_name,
                "event_time": int(time.time()),
                "user_data": user_data,
                "action_source": "chat",
                "event_source_url": "https://love-doc-ai.com/chat",
                "custom_data": custom_data or {}
            }
            
            # Add session ID to custom data
            event_data["custom_data"]["session_id"] = session_id
            
            # Prepare API payload
            payload = {
                "data": [event_data]
            }
            
            # Add test event code if configured (for testing in Events Manager)
            if self.test_event_code:
                payload["test_event_code"] = self.test_event_code
                logger.info(f"🧪 Sending test event with code: {self.test_event_code}")
            
            # Send request to Conversions API
            url = f"{self.base_url}/{self.pixel_id}/events"
            params = {"access_token": self.access_token}
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url,
                    params=params,
                    json=payload,
                    timeout=10.0
                )
                response.raise_for_status()
                
                result = response.json()
                events_received = result.get("events_received", 0)
                
                if events_received > 0:
                    logger.info(f"✅ Meta Pixel event sent: {event_name} for session {session_id[:20]}...")
                    return True
                else:
                    logger.warning(f"⚠️ Meta Pixel event not received: {event_name}")
                    return False
        
        except httpx.HTTPError as e:
            logger.error(f"❌ HTTP error sending Meta Pixel event {event_name}: {e}")
            if hasattr(e, 'response') and e.response:
                logger.error(f"Response: {e.response.text}")
            return False
        
        except Exception as e:
            logger.error(f"❌ Unexpected error sending Meta Pixel event {event_name}: {e}")
            return False
    
    async def send_chat_started_event(
        self,
        user_id: str,
        session_id: str,
        coach_type: str,
        conversation_id: str,
        client_ip: Optional[str] = None,
        client_user_agent: Optional[str] = None,
        user_data: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Send Chat Started event (fires on first message in session).
        
        Args:
            user_id: User UID
            session_id: Unique session identifier
            coach_type: Type of coach (kabir, tara, vikram)
            conversation_id: Conversation ID
            client_ip: Client IP address (optional)
            client_user_agent: Client user agent (optional)
            user_data: Additional user data (email, phone, name, gender, etc.) (optional)
            
        Returns:
            True if successful, False otherwise
        """
        custom_data = {
            "coach_type": coach_type,
            "conversation_id": conversation_id,
            "message_count": 1,
            "source": "love-doc-ai"
        }
        
        # Extract user data fields if provided
        extra_user_data = user_data or {}
        
        return await self.send_server_event(
            event_name="ChatStarted",
            user_id=user_id,
            session_id=session_id,
            custom_data=custom_data,
            client_ip=client_ip,
            client_user_agent=client_user_agent,
            email=extra_user_data.get("email"),
            phone=extra_user_data.get("phone"),
            first_name=extra_user_data.get("first_name"),
            last_name=extra_user_data.get("last_name"),
            gender=extra_user_data.get("gender"),
            date_of_birth=extra_user_data.get("date_of_birth"),
            city=extra_user_data.get("city"),
            state=extra_user_data.get("state"),
            zip_code=extra_user_data.get("zip_code"),
            country=extra_user_data.get("country")
        )
    
    async def send_chat_engaged_event(
        self,
        user_id: str,
        session_id: str,
        coach_type: str,
        conversation_id: str,
        client_ip: Optional[str] = None,
        client_user_agent: Optional[str] = None,
        user_data: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Send Chat Engaged event (fires on third message in session).
        
        Args:
            user_id: User UID
            session_id: Unique session identifier
            coach_type: Type of coach (kabir, tara, vikram)
            conversation_id: Conversation ID
            client_ip: Client IP address (optional)
            client_user_agent: Client user agent (optional)
            user_data: Additional user data (email, phone, name, gender, etc.) (optional)
            
        Returns:
            True if successful, False otherwise
        """
        custom_data = {
            "coach_type": coach_type,
            "conversation_id": conversation_id,
            "message_count": 3,
            "source": "love-doc-ai"
        }
        
        # Extract user data fields if provided
        extra_user_data = user_data or {}
        
        return await self.send_server_event(
            event_name="ChatEngaged",
            user_id=user_id,
            session_id=session_id,
            custom_data=custom_data,
            client_ip=client_ip,
            client_user_agent=client_user_agent,
            email=extra_user_data.get("email"),
            phone=extra_user_data.get("phone"),
            first_name=extra_user_data.get("first_name"),
            last_name=extra_user_data.get("last_name"),
            gender=extra_user_data.get("gender"),
            date_of_birth=extra_user_data.get("date_of_birth"),
            city=extra_user_data.get("city"),
            state=extra_user_data.get("state"),
            zip_code=extra_user_data.get("zip_code"),
            country=extra_user_data.get("country")
        )


# Singleton instance for convenience
meta_pixel_client = MetaPixelClient()


