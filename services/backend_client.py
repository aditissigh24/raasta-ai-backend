"""
Backend client for fetching user configuration from the backend service.
"""
import httpx
from typing import TypedDict, Optional, List, Dict
import logging
from config.settings import settings

logger = logging.getLogger(__name__)


class UserConfig(TypedDict):
    """User configuration returned from backend."""
    user_id: str
    name: str
    gender: str
    age: int
    currentSituation: str
    situations: list


class BackendClient:
    """HTTP client for communicating with the backend service."""

    def __init__(self, base_url: str | None = None, timeout: float = 30.0):
        self.base_url = base_url or settings.BACKEND_BASE_URL
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

        # Coach data cache -- populated by load_coaches() at startup or on
        # first lookup.  Keyed by coach_type (lowercase firstName).
        self._coaches_by_type: Dict[str, dict] = {}
        self._coaches_by_id: Dict[int, dict] = {}
        self._coaches_loaded = False

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the async HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
            )
        return self._client

    async def close(self):
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Coach data – fetch, cache, and lookup
    # ------------------------------------------------------------------

    async def fetch_coaches(self) -> list[dict]:
        """Fetch list of active coaches from backend."""
        client = await self._get_client()
        response = await client.get("/api/coaches/list-all")
        response.raise_for_status()
        data = response.json()
        coaches = data.get("coaches", [])
        logger.info(f"Fetched {len(coaches)} coaches from backend")
        return coaches

    async def load_coaches(self) -> None:
        """Fetch coaches from the backend and populate the local cache.

        Safe to call multiple times; the cache is simply refreshed.
        """
        coaches = await self.fetch_coaches()
        by_type: Dict[str, dict] = {}
        by_id: Dict[int, dict] = {}

        for coach in coaches:
            first_name = (coach.get("firstName") or "").strip().lower()
            if not first_name:
                name = (coach.get("name") or "").strip()
                first_name = name.split()[0].lower() if name else ""
            if not first_name:
                continue

            coach["_coach_type"] = first_name
            by_type[first_name] = coach

            db_id = coach.get("id")
            if db_id is not None:
                by_id[int(db_id)] = coach

        self._coaches_by_type = by_type
        self._coaches_by_id = by_id
        self._coaches_loaded = True
        logger.info(f"Coach cache loaded: {len(by_type)} by type, {len(by_id)} by id")

    async def _ensure_coaches_loaded(self) -> None:
        if not self._coaches_loaded:
            await self.load_coaches()

    async def get_coach_by_type(self, coach_type: str) -> Optional[dict]:
        """Return the full backend coach dict for a given coach_type key."""
        await self._ensure_coaches_loaded()
        return self._coaches_by_type.get(coach_type.lower())

    async def get_coach_by_id(self, db_id: int) -> Optional[dict]:
        """Return the full backend coach dict for a given database id."""
        await self._ensure_coaches_loaded()
        return self._coaches_by_id.get(db_id)

    async def resolve_coach_type(self, coach_id) -> Optional[str]:
        """Resolve any coachId value (int, str-digit, or string name) to a
        coach_type key (lowercase firstName).

        Returns None if resolution fails.
        """
        await self._ensure_coaches_loaded()

        # Integer or numeric string -> lookup by DB id
        try:
            int_id = int(coach_id)
            coach = self._coaches_by_id.get(int_id)
            if coach:
                return coach["_coach_type"]
        except (ValueError, TypeError):
            pass

        # String -> try direct match or substring match against known types
        if isinstance(coach_id, str):
            lower = coach_id.lower()
            if lower in self._coaches_by_type:
                return lower
            for ctype in self._coaches_by_type:
                if ctype in lower:
                    return ctype

        return None

    def get_all_coaches(self) -> list[dict]:
        """Return all cached coach dicts (empty list if not yet loaded)."""
        return list(self._coaches_by_type.values())

    async def fetch_user_config(self, user_id: str) -> UserConfig:
        """
        Fetch user configuration from the backend service.
        
        Args:
            user_id: The user's unique identifier
            
        Returns:
            UserConfig containing user's name, gender, and age
            
        Raises:
            httpx.HTTPStatusError: If the request fails
        """
        client = await self._get_client()
        
        endpoint = settings.BACKEND_USER_CONFIG_ENDPOINT.format(user_id=user_id)
        
        response = await client.get(endpoint)
        response.raise_for_status()
        
        data = response.json()
        user = data.get("user", data)
        logger.info(f"user details are {data}")

        return UserConfig(
            user_id=user_id,
            name=user.get("name", "Friend"),
            gender=user.get("gender", "unknown"),
            age=user.get("ageRange", 25),
            currentSituation=user.get("currentSituation", ""),
            situations=user.get("situations", [])
        )
    
    async def update_user_details(self, user_id: str, updates: Dict) -> bool:
        """
        Update user configuration fields in the backend service.
        
        Args:
            user_id: The user's unique identifier
            updates: Dictionary of fields to update (partial update)
                    Can include: name, gender, ageRange, currentSituation, situations
            
        Returns:
            True if successful, False otherwise
        """
        try:
            client = await self._get_client()
            
            headers = {}
            if settings.BACKEND_API_KEY:
                headers["x-api-key"] = settings.BACKEND_API_KEY
            
            endpoint = settings.BACKEND_USER_UPDATE_ENDPOINT.format(user_id=user_id)
            
            # Only send fields that have values
            payload = {k: v for k, v in updates.items() if v}
            
            if not payload:
                logger.warning(f"No valid fields to update for user {user_id}")
                return False
            
            response = await client.patch(
                endpoint,
                json=payload,
                headers=headers
            )
            response.raise_for_status()
            
            logger.info(f"✅ Updated user details for {user_id}: {list(payload.keys())}")
            return True
            
        except httpx.HTTPError as e:
            logger.error(f"Failed to update user details for {user_id}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error updating user details: {e}")
            return False
    
    async def fetch_all_data(self) -> dict:
        """
        Fetch all conversations, messages, and user data from the backend.
        
        Returns:
            Dictionary containing conversations, messages, and users data
            
        Raises:
            httpx.HTTPStatusError: If the request fails
        """
        try:
            client = await self._get_client()
            
            headers = {}
            if settings.BACKEND_API_KEY:
                headers["x-api-key"] = settings.BACKEND_API_KEY
            
            response = await client.get(
                "/api/admin/all-data",
                headers=headers
            )
            response.raise_for_status()
            
            data = response.json()
            logger.info(f"Fetched all data: {len(data.get('conversations', []))} conversations, "
                       f"{len(data.get('messages', []))} messages, "
                       f"{len(data.get('users', []))} users")
            
            return data
            
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch all data: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error fetching all data: {e}")
            raise
    
    async def upsert_conversation(
        self, 
        user_id: str, 
        coach_id: int, 
        chatroom_id: str
    ) -> Optional[str]:
        """
        Create or update a conversation record in the database.
        
        Args:
            user_id: User UID
            coach_id: Coach's database ID (integer)
            chatroom_id: Conversation/chatroom ID
            
        Returns:
            The conversation database ID if successful, None otherwise
        """
        try:
            client = await self._get_client()
            
            headers = {}
            if settings.BACKEND_API_KEY:
                headers["x-api-key"] = settings.BACKEND_API_KEY
            
            payload = {
                "userId": user_id,
                "coachId": coach_id,
                "chatroomId": chatroom_id
            }
            
            response = await client.post(
                "/api/chat/conversation/upsert",
                json=payload,
                headers=headers
            )
            response.raise_for_status()
            
            data = response.json()
            conversation_id = data.get("conversation", {}).get("id")
            
            if conversation_id:
                logger.info(f"📝 Conversation upserted: {conversation_id} for user {user_id}")
            else:
                logger.warning(f"Conversation upsert response missing 'id' field")
                
            return conversation_id
            
        except httpx.HTTPError as e:
            logger.error(f"Failed to upsert conversation for user {user_id}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error upserting conversation: {e}")
            return None
    
    async def create_message(
        self,
        conversation_id: str,
        sender_id: str,
        message_text: str,
        cometchat_message_id: Optional[str] = None,
        session_id: Optional[str] = None,
        sender_type : str = "USER",
        message_type : str = "TEXT"
    ) -> bool:
        """
        Create a message record in the database.
        
        Args:
            conversation_id: Database conversation ID
            sender_id: UID of the sender
            message_text: The message text content
            cometchat_message_id: External message ID (for deduplication)
            session_id: Session identifier from resource field (for tracking)
            
        Returns:
            True if successful (including duplicates), False otherwise
        """
        try:
            client = await self._get_client()
            
            headers = {}
            if settings.BACKEND_API_KEY:
                headers["x-api-key"] = settings.BACKEND_API_KEY
            
            payload = {
                "conversationId": conversation_id,
                "senderType": sender_type,
                "senderId": sender_id,
                "messageType": message_type.upper(),
                "text": message_text,
                "CometchatMessageId": cometchat_message_id,
                "sessionId": session_id,
                "attachmentUrl": None
            }
            logger.info(f"payload is {payload}")
            
            response = await client.post(
                "/api/chat/message/create",
                json=payload,
                headers=headers
            )
            response.raise_for_status()
            
            data = response.json()
            is_duplicate = data.get("duplicate", False)
            message_id = data.get("messageId")
            
            if is_duplicate:
                logger.info(f"📝 Message already exists: {message_id}")
            else:
                logger.info(f"📝 Message created: {message_id} in conversation {conversation_id}")
                
            return True
            
        except httpx.HTTPError as e:
            logger.error(f"Failed to create message in conversation {conversation_id}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error creating message: {e}")
            return False
    
    async def fetch_user_message_count(self, chatroom_id: str) -> int:
        """
        Return the number of USER-sent messages stored in the DB for a chatroom,
        across all sessions.  Used to decide when to fire chat_started (count==0)
        and chat_engaged (count==2) analytics events.

        Expects the backend to expose:
            GET /api/chat/messages/count?chatroomId=<id>&senderType=USER
        Response body: { "count": <int> }

        Returns -1 on any error so that callers can safely skip event firing.
        """
        try:
            client = await self._get_client()
            headers = {}
            if settings.BACKEND_API_KEY:
                headers["x-api-key"] = settings.BACKEND_API_KEY
            response = await client.get(
                "/api/chat/messages/count",
                params={"chatroomId": chatroom_id, "senderType": "USER"},
                headers=headers,
            )
            response.raise_for_status()
            count = response.json().get("count", -1)
            logger.info(f"📊 DB user message count for chatroom {chatroom_id}: {count}")
            return int(count)
        except Exception as e:
            logger.error(f"Failed to fetch user message count for chatroom {chatroom_id}: {e}")
            return -1

    async def fetch_messages_by_session(self, session_id: str) -> List[Dict]:
        """
        Fetch all messages for a given session ID.
        
        Args:
            session_id: The session identifier
            
        Returns:
            List of message dictionaries with sender info and text
        """
        try:
            client = await self._get_client()
            
            headers = {}
            if settings.BACKEND_API_KEY:
                headers["x-api-key"] = settings.BACKEND_API_KEY
            
            response = await client.get(
                f"/api/chat/messages/by-session",
                params={"sessionId": session_id},
                headers=headers
            )
            response.raise_for_status()
            
            data = response.json()
            messages = data.get("messages", [])
            
            logger.info(f"📋 Fetched {len(messages)} messages for session {session_id[:30]}...")
            return messages
            
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch messages for session {session_id}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error fetching messages by session: {e}")
            return []
    
    async def create_session_summary(
        self,
        user_id: str,
        coach_id: int,
        conversation_id:str,
        session_id: str,
        summary_text: str
    ) -> bool:
        """
        Create a session summary record in the database.
        
        Args:
            user_id: User UID
            coach_id: Coach's database ID
            conversation_id: Conversation ID
            session_id: Session identifier
            summary_text: The summary text (300-400 characters)
            
        Returns:
            True if successful, False otherwise
        """
        try:
            client = await self._get_client()
            
            headers = {}
            if settings.BACKEND_API_KEY:
                headers["x-api-key"] = settings.BACKEND_API_KEY
            
            payload = {
                "userId": user_id,
                "coachId": coach_id,
                "conversationId": conversation_id,
                "sessionId": session_id,
                "summaryText": summary_text
            }
            logger.info(f"payload for summary creation: {payload}")
            response = await client.post(
                "/api/chat/session-summary/create",
                json=payload,
                headers=headers
            )
            response.raise_for_status()
            
            data = response.json()
            summary_id = data.get("summaryId")
            
            logger.info(f"📝 Session summary created: {summary_id} for session {session_id[:30]}...")
            return True
            
        except httpx.HTTPError as e:
            logger.error(f"Failed to create session summary for {session_id}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error creating session summary: {e}")
            return False
    
    async def fetch_recent_session_summaries(
        self,
        user_id: str,
        conversation_id: str,
        limit: int = 3
    ) -> List[Dict]:
        """
        Fetch recent session summaries for a user.
        
        Args:
            user_id: User UID
            limit: Maximum number of summaries to fetch (default: 3)
            
        Returns:
            List of session summary dictionaries
        """
        try:
            client = await self._get_client()
            
            headers = {}
            if settings.BACKEND_API_KEY:
                headers["x-api-key"] = settings.BACKEND_API_KEY
            
            response = await client.get(
                "/api/chat/session-summaries",
                params={ "conversationId":conversation_id, "limit": limit},
                headers=headers
            )
            response.raise_for_status()
            
            data = response.json()
            summaries = data.get("summaries", [])
            
            logger.info(f"📚 Fetched {len(summaries)} session summaries for user {user_id}")
            return summaries
            
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch session summaries for user {user_id}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error fetching session summaries: {e}")
            return []
    
    async def __aenter__(self):
        """Async context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()


# Singleton instance for convenience
backend_client = BackendClient()


