"""
Application settings and environment configuration.
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    """Application settings loaded from environment variables."""
    
    # OpenAI Configuration
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4.1")
    
    # Backend Service Configuration
    BACKEND_BASE_URL: str = os.getenv("BACKEND_BASE_URL", "http://localhost:3000")
    BACKEND_USER_CONFIG_ENDPOINT: str = "/api/users/{user_id}"
    BACKEND_USER_UPDATE_ENDPOINT: str = "/api/users/{user_id}"
    BACKEND_API_KEY: str = os.getenv("BACKEND_API_KEY", "")
    
    # Redis Configuration (for background task coordination)
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    REDIS_TASK_TTL: int = int(os.getenv("REDIS_TASK_TTL", "300"))
    
    # Meta Pixel Configuration (for event tracking)
    META_PIXEL_ID: str = os.getenv("META_PIXEL_ID", "")
    META_PIXEL_ACCESS_TOKEN: str = os.getenv("META_PIXEL_ACCESS_TOKEN", "")
    META_PIXEL_TEST_EVENT_CODE: str = os.getenv("META_PIXEL_TEST_EVENT_CODE", "")
    
    # Mixpanel Configuration (for event tracking)
    MIXPANEL_PROJECT_TOKEN: str = os.getenv("MIXPANEL_PROJECT_TOKEN", "")
    
    # MongoDB Configuration (for event storage)
    MONGODB_URI: str = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    MONGODB_DATABASE: str = os.getenv("MONGODB_DATABASE", "love_doc_events")
    
    # API Key Authentication (for event API)
    EVENT_API_KEYS: str = os.getenv("EVENT_API_KEYS", "")
    EVENT_API_URL: str = os.getenv("EVENT_API_URL", "http://localhost:8080/api/v1/events")

    # AI Kill Switch
    AI_RESPONSES_ENABLED: bool = os.getenv("AI_RESPONSES_ENABLED", "true").lower() == "true"

    # Coach Types
    COACH_TYPES: list = ["kabir", "tara", "vikram"]

    # Socket.IO worker (direct connection to socket-server)
    SOCKET_SERVER_URL: str = os.getenv("SOCKET_SERVER_URL", "")
    AI_WORKER_SECRET: str = os.getenv("AI_WORKER_SECRET", "")

    # PostgreSQL direct access
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")



settings = Settings()


