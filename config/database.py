"""
MongoDB async client for event storage.
Provides connection pooling, health checks, and collection access.
"""
import logging
from typing import Optional
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase, AsyncIOMotorCollection
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

from config.settings import settings

logger = logging.getLogger(__name__)


class MongoDBClient:
    """Async MongoDB client with connection pooling and error handling."""
    
    def __init__(self, mongodb_uri: str, database_name: str):
        """
        Initialize MongoDB client.
        
        Args:
            mongodb_uri: MongoDB connection URI
            database_name: Name of the database to use
        """
        self.mongodb_uri = mongodb_uri
        self.database_name = database_name
        self._client: Optional[AsyncIOMotorClient] = None
        self._database: Optional[AsyncIOMotorDatabase] = None
        self._is_available = False
    
    async def connect(self) -> bool:
        """
        Initialize MongoDB connection.
        
        Returns:
            True if connection successful, False otherwise
        """
        try:
            # Close existing client if any before reconnecting
            if self._client:
                self._client.close()
                self._client = None
                self._database = None
                
            # Create MongoDB client with connection pooling
            self._client = AsyncIOMotorClient(
                self.mongodb_uri,
                maxPoolSize=100,
                minPoolSize=10,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
                socketTimeoutMS=5000
            )
            
            # Get database reference
            self._database = self._client[self.database_name]
            
            # Test connection with ping
            await self._client.admin.command('ping')
            
            self._is_available = True
            logger.info(f"✅ MongoDB connected successfully: {self.database_name}")
            return True
            
        except (ConnectionFailure, ServerSelectionTimeoutError) as e:
            logger.error(f"❌ MongoDB connection failed: {e}")
            self._is_available = False
            return False
        except Exception as e:
            logger.error(f"❌ Unexpected error connecting to MongoDB: {e}")
            self._is_available = False
            return False
    
    async def close(self):
        """Close MongoDB connection."""
        if self._client:
            try:
                self._client.close()
                logger.info("MongoDB connection closed")
            except Exception as e:
                logger.error(f"Error closing MongoDB connection: {e}")
        
        self._is_available = False
    
    @property
    def is_available(self) -> bool:
        """Check if MongoDB is available."""
        return self._is_available
    
    @property
    def database(self) -> Optional[AsyncIOMotorDatabase]:
        """Get database reference."""
        if not self._is_available:
            logger.warning("MongoDB is not available")
            return None
        return self._database
    
    async def get_collection(self, collection_name: str) -> AsyncIOMotorCollection:
        """
        Get a MongoDB collection reference.
        
        Args:
            collection_name: Name of the collection
            
        Returns:
            Collection reference
            
        Raises:
            RuntimeError: If MongoDB is not available
        """
        if not self._is_available or self._database is None:
            logger.warning("MongoDB unavailable, attempting reconnect...")
            reconnected = await self.connect()  # try to reconnect
            if not reconnected:
                raise RuntimeError("MongoDB is not connected")
    
        
        return self._database[collection_name]
    
    async def health_check(self) -> bool:
        """
        Check if MongoDB is healthy.
        
        Returns:
            True if MongoDB is healthy, False otherwise
        """
        if not self._is_available or not self._client:
            return await self.connect() 
        
        try:
            # Ping the server
            await self._client.admin.command('ping')
            return True
        except Exception as e:
            logger.error(f"MongoDB health check failed: {e}")
            self._is_available = False
            return await self.connect()


# Singleton instance
mongodb_client = MongoDBClient(
    mongodb_uri=settings.MONGODB_URI,
    database_name=settings.MONGODB_DATABASE
)


async def get_collection(collection_name: str) -> AsyncIOMotorCollection:
    """
    Helper function to get a collection.
    
    Args:
        collection_name: Name of the collection
        
    Returns:
        Collection reference
    """
    return await mongodb_client.get_collection(collection_name)
