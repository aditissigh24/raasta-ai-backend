"""
Redis async client for background task coordination and pub/sub messaging.
Provides connection pooling, retry logic, and graceful degradation.
"""
import json
import logging
import asyncio
from typing import Optional, Dict, Set, List, Callable, Awaitable
from redis.asyncio import Redis, ConnectionPool
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError, ConnectionError as RedisConnectionError

from config.settings import settings

logger = logging.getLogger(__name__)


class RedisClient:
    """Async Redis client with connection pooling and error handling."""

    PREFIX = "raasta:"

    def __init__(self, redis_url: str):
        """
        Initialize Redis client.
        
        Args:
            redis_url: Redis connection URL (e.g., redis://localhost:6379/0)
        """
        self.redis_url = redis_url
        self._pool: Optional[ConnectionPool] = None
        self._client: Optional[Redis] = None
        self._is_available = False
        
        # In-memory fallback for counters when Redis is unavailable
        self._memory_counters: Dict[str, int] = {}
    
    async def connect(self) -> bool:
        """
        Initialize Redis connection pool.
        
        Returns:
            True if connection successful, False otherwise
        """
        try:
            # Create connection pool
            self._pool = ConnectionPool.from_url(
                self.redis_url,
                decode_responses=True,
                max_connections=10,
                socket_connect_timeout=5,
                socket_timeout=5,
                retry_on_timeout=True
            )
            
            # Create Redis client
            self._client = Redis(connection_pool=self._pool)
            
            # Test connection
            await self._client.ping()
            
            self._is_available = True
            logger.info(f"✅ Redis connected successfully: {self.redis_url}")
            return True
            
        except (RedisError, RedisConnectionError) as e:
            logger.warning(f"⚠️ Redis connection failed: {e}. Running in degraded mode (in-memory only)")
            self._is_available = False
            return False
        except Exception as e:
            logger.error(f"❌ Unexpected error connecting to Redis: {e}")
            self._is_available = False
            return False
    
    async def close(self):
        """Close Redis connection pool."""
        if self._client:
            try:
                await self._client.aclose()
                logger.info("Redis connection closed")
            except Exception as e:
                logger.error(f"Error closing Redis connection: {e}")
        
        if self._pool:
            try:
                await self._pool.aclose()
            except Exception as e:
                logger.error(f"Error closing Redis pool: {e}")
        
        self._is_available = False
    
    @property
    def is_available(self) -> bool:
        """Check if Redis is available."""
        return self._is_available
    
    async def set(self, key: str, value: str, ex: Optional[int] = None) -> bool:
        """
        Set a key-value pair with optional expiration.
        
        Args:
            key: Redis key
            value: Value to store
            ex: Expiration time in seconds (optional)
            
        Returns:
            True if successful, False otherwise
        """
        if not self._is_available or not self._client:
            logger.debug(f"Redis unavailable, skipping SET {key}")
            return False
        
        try:
            await self._client.set(key, value, ex=ex)
            return True
        except RedisError as e:
            logger.warning(f"Redis SET error for key {key}: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error in Redis SET: {e}")
            return False
    
    async def get(self, key: str) -> Optional[str]:
        """
        Get value for a key.
        
        Args:
            key: Redis key
            
        Returns:
            Value if exists, None otherwise
        """
        if not self._is_available or not self._client:
            logger.debug(f"Redis unavailable, skipping GET {key}")
            return None
        
        try:
            return await self._client.get(key)
        except RedisError as e:
            logger.warning(f"Redis GET error for key {key}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in Redis GET: {e}")
            return None
    
    async def delete(self, key: str) -> bool:
        """
        Delete a key.
        
        Args:
            key: Redis key to delete
            
        Returns:
            True if successful, False otherwise
        """
        if not self._is_available or not self._client:
            logger.debug(f"Redis unavailable, skipping DELETE {key}")
            return False
        
        try:
            await self._client.delete(key)
            return True
        except RedisError as e:
            logger.warning(f"Redis DELETE error for key {key}: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error in Redis DELETE: {e}")
            return False
    
    async def exists(self, key: str) -> bool:
        """
        Check if a key exists.
        
        Args:
            key: Redis key
            
        Returns:
            True if key exists, False otherwise
        """
        if not self._is_available or not self._client:
            return False
        
        try:
            result = await self._client.exists(key)
            return bool(result)
        except RedisError as e:
            logger.warning(f"Redis EXISTS error for key {key}: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error in Redis EXISTS: {e}")
            return False
    
    async def health_check(self) -> bool:
        """
        Check if Redis is healthy.
        
        Returns:
            True if Redis is healthy, False otherwise
        """
        if not self._is_available or not self._client:
            return False
        
        try:
            await self._client.ping()
            return True
        except Exception as e:
            logger.error(f"Redis health check failed: {e}")
            self._is_available = False
            return False
    
    async def increment_counter(self, key: str, ttl: Optional[int] = None) -> int:
        """
        Atomically increment a counter and return the new value.
        Falls back to in-memory counter if Redis is unavailable.
        
        Args:
            key: Redis key for the counter
            ttl: Time-to-live in seconds (optional)
            
        Returns:
            The new counter value after incrementing
        """
        if self._is_available and self._client:
            try:
                # Use Redis INCR for atomic increment
                new_value = await self._client.incr(key)
                
                # Set TTL if provided and this is the first increment
                if ttl and new_value == 1:
                    await self._client.expire(key, ttl)
                
                logger.debug(f"Redis counter incremented: {key} = {new_value}")
                return new_value
                
            except RedisError as e:
                logger.warning(f"Redis INCR error for key {key}: {e}. Using in-memory fallback.")
                self._is_available = False
                # Fall through to in-memory fallback
            except Exception as e:
                logger.error(f"Unexpected error in Redis INCR: {e}. Using in-memory fallback.")
                # Fall through to in-memory fallback
        
        # In-memory fallback
        if key not in self._memory_counters:
            self._memory_counters[key] = 0
        
        self._memory_counters[key] += 1
        new_value = self._memory_counters[key]
        
        logger.debug(f"In-memory counter incremented: {key} = {new_value}")
        return new_value
    
    async def get_user_session(self, user_id: str) -> Optional[str]:
        """
        Get the active session ID for a user.

        Args:
            user_id: User UID

        Returns:
            Session ID if exists, None otherwise
        """
        key = f"{self.PREFIX}session:user:{user_id}"
        return await self.get(key)

    async def set_user_session(self, user_id: str, session_id: str, ttl: int = 86400) -> bool:
        """
        Store a session ID for a user.

        Args:
            user_id: User UID
            session_id: Session identifier
            ttl: Time-to-live in seconds (default: 24 hours)

        Returns:
            True if successful, False otherwise
        """
        key = f"{self.PREFIX}session:user:{user_id}"
        return await self.set(key, session_id, ex=ttl)

    async def delete_user_session(self, user_id: str) -> bool:
        """
        Delete a user's session ID.

        Args:
            user_id: User UID

        Returns:
            True if successful, False otherwise
        """
        key = f"{self.PREFIX}session:user:{user_id}"
        return await self.delete(key)

    # --- Per-user active coaches set (for per-user+coach session tracking) ---

    async def sadd_user_active_coach(self, user_uid: str, coach_uid: str) -> bool:
        """Track that a user has an active session with a specific coach."""
        key = f"{self.PREFIX}active_coaches:{user_uid}"
        if not self._is_available or not self._client:
            logger.debug("Redis unavailable, skipping SADD active_coaches")
            return False
        try:
            await self._client.sadd(key, coach_uid)
            return True
        except RedisError as e:
            logger.warning(f"Redis SADD error for {key}: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error in Redis SADD for {key}: {e}")
            return False

    async def smembers_user_active_coaches(self, user_uid: str) -> Set[str]:
        """Return all coach UIDs that a user has active sessions with."""
        key = f"{self.PREFIX}active_coaches:{user_uid}"
        if not self._is_available or not self._client:
            return set()
        try:
            members = await self._client.smembers(key)
            return set(members) if members else set()
        except RedisError as e:
            logger.warning(f"Redis SMEMBERS error for {key}: {e}")
            return set()
        except Exception as e:
            logger.error(f"Unexpected error in Redis SMEMBERS for {key}: {e}")
            return set()

    async def delete_user_active_coaches(self, user_uid: str) -> bool:
        """Remove the active coaches tracking set for a user (called on disconnect)."""
        key = f"{self.PREFIX}active_coaches:{user_uid}"
        if not self._is_available or not self._client:
            return False
        try:
            await self._client.delete(key)
            return True
        except RedisError as e:
            logger.warning(f"Redis DELETE error for {key}: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error in Redis DELETE for {key}: {e}")
            return False

    # --- Pub/Sub ---

    async def publish(self, channel: str, message: dict) -> bool:
        """Publish a JSON message to a Redis channel."""
        if not self._is_available or not self._client:
            logger.error(f"Redis unavailable, cannot PUBLISH to {channel}")
            return False
        try:
            await self._client.publish(channel, json.dumps(message))
            logger.info(f"📤 Published to {channel}")
            return True
        except RedisError as e:
            logger.error(f"Redis PUBLISH error on {channel}: {e}")
            return False

    async def subscribe(self, channels: List[str]) -> Optional[PubSub]:
        """Create a PubSub subscription on the given channels.

        Returns the PubSub object so the caller can iterate over messages.
        Uses a dedicated connection (not the pooled one) because subscribe
        blocks the connection.
        """
        if not self._is_available or not self._client:
            logger.error("Redis unavailable, cannot subscribe")
            return None
        try:
            pubsub = self._client.pubsub()
            await pubsub.subscribe(*channels)
            logger.info(f"📡 Subscribed to Redis channels: {channels}")
            return pubsub
        except RedisError as e:
            logger.error(f"Redis subscribe error: {e}")
            return None


# Singleton instance
redis_client = RedisClient(redis_url=settings.REDIS_URL)



