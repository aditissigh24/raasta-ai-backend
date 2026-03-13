"""
Love Coach AI - FastAPI Application with Redis Pub/Sub Integration.

A relationship coaching chatbot with multiple coach personas.
Subscribes to raasta:ai:request Redis channel, runs LLM, and publishes
replies to raasta:ai:response.
"""
import asyncio
import logging
import uvicorn
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from config.settings import settings
from services.webhook_handler import start_redis_subscriber, get_or_create_session_id
from services.backend_client import backend_client
from app.routes.events import router as events_router

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Background tasks
_redis_subscriber_task: Optional[asyncio.Task] = None


# Create FastAPI app
app = FastAPI(
    title="Love Coach AI",
    description="AI-powered relationship coaching chatbot with Redis pub/sub integration",
    version="2.0.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.lovedr.in","https://lovedr.in","https://coach.lovedr.in","https://www.raasta.today","https://raasta.today" ,"https://stage-love-doc-ai.lovedr.in","http://localhost:3001","http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Strict-Transport-Security"] = "max-age=16070400; includeSubDomains"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Content-Security-Policy"] = "default-src 'self'"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store, no-cache, max-age=0"
        response.headers["Server"] = ""
        return response


app.add_middleware(SecurityHeadersMiddleware)

# Include API routers
app.include_router(events_router)


# Request/Response models
class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    timestamp: str
    openai_configured: bool
    backend_configured: bool
    redis_connected: bool
    mongodb_configured: bool


class CoachInfo(BaseModel):
    """Coach information."""
    id: int
    name: str
    specialty: str
    tagline: str

# Available coaches
COACHES = []


@app.get("/", response_model=dict)
async def root():
    """Root endpoint with API information."""
    return {
        "name": "Love Coach AI",
        "version": "2.0.0",
        "description": "AI-powered relationship coaching chatbot with Redis pub/sub integration",
        "docs": "/docs"
    }


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    from config.redis_client import redis_client
    redis_healthy = await redis_client.health_check()

    from config.database import mongodb_client
    mongo_healthy = await mongodb_client.health_check()

    return HealthResponse(
        status="healthy",
        timestamp=datetime.utcnow().isoformat(),
        openai_configured=bool(settings.OPENAI_API_KEY),
        backend_configured=bool(settings.BACKEND_BASE_URL),
        redis_connected=redis_healthy,
        mongodb_configured=mongo_healthy
    )


@app.get("/coaches", response_model=list[CoachInfo])
async def get_coaches():
    """Get list of available coaches."""
    return COACHES


@app.get("/coaches/{coach_id}", response_model=CoachInfo)
async def get_coach(coach_id: str):
    """Get information about a specific coach."""
    for coach in COACHES:
        if coach.id == coach_id:
            return coach
    raise HTTPException(status_code=404, detail=f"Coach '{coach_id}' not found")


@app.get("/session")
async def get_session(user_uid: str, coach_uid: str):
    """
    Return the canonical session ID for a user+coach pair.

    Creates a new session ID (UUID v4) the first time a pair is seen, then
    returns the same ID on every subsequent call until the session expires
    (24 h from last activity) or the user disconnects and the grace period
    elapses.
    """
    if not user_uid or not coach_uid:
        raise HTTPException(status_code=422, detail="Both user_uid and coach_uid are required")

    session_id, created = await get_or_create_session_id(user_uid, coach_uid)
    return {
        "session_id": session_id,
        "user_uid": user_uid,
        "coach_uid": coach_uid,
        "created": created,
    }


async def run_mongodb_watchdog():
    """Periodically ping MongoDB and auto-reconnect if needed."""
    while True:
        await asyncio.sleep(60)
        from config.database import mongodb_client
        healthy = await mongodb_client.health_check()
        if not healthy:
            logger.warning("MongoDB watchdog: connection lost, attempting reconnect...")
            await mongodb_client.connect()


@app.on_event("startup")
async def startup_event():
    """Handle application startup."""
    logger.info("Starting Love Coach AI with Redis Pub/Sub Integration...")
    logger.info(f"OpenAI Model: {settings.OPENAI_MODEL}")
    logger.info(f"Backend URL: {settings.BACKEND_BASE_URL}")

    # Initialize Redis connection
    from config.redis_client import redis_client
    logger.info(f"Connecting to Redis: {settings.REDIS_URL}")
    redis_connected = await redis_client.connect()
    if redis_connected:
        logger.info(f"Redis task TTL: {settings.REDIS_TASK_TTL}s")
    else:
        logger.warning("Running without Redis (in-memory task tracking only)")

    # Initialize MongoDB connection
    from config.database import mongodb_client
    logger.info(f"Connecting to MongoDB: {settings.MONGODB_DATABASE}")
    mongo_connected = await mongodb_client.connect()
    if mongo_connected:
        logger.info("MongoDB connected successfully")
    else:
        logger.warning("MongoDB connection failed")

    asyncio.create_task(run_mongodb_watchdog())

    # Fetch coaches from backend and populate the shared cache
    try:
        global COACHES
        await backend_client.load_coaches()
        COACHES = [
            CoachInfo(
                id=coach["id"],
                name=coach["name"],
                specialty=coach.get("specialty", ""),
                tagline=coach.get("bio", "")[:100],
            )
            for coach in backend_client.get_all_coaches()
        ]
        logger.info(f"Loaded {len(COACHES)} coaches into API + cache")
    except Exception as e:
        logger.warning(f"Failed to fetch coaches from backend: {e}. Using defaults.")

    # Start Redis pub/sub subscriber for ai:request and ai:disconnect
    global _redis_subscriber_task
    _redis_subscriber_task = asyncio.create_task(start_redis_subscriber())
    logger.info("Started Redis pub/sub subscriber (raasta:ai:request, raasta:ai:disconnect)")


@app.on_event("shutdown")
async def shutdown_event():
    """Handle application shutdown."""
    logger.info("Shutting down Love Coach AI...")

    # Cancel Redis subscriber
    global _redis_subscriber_task
    if _redis_subscriber_task is not None:
        _redis_subscriber_task.cancel()
        try:
            await _redis_subscriber_task
        except asyncio.CancelledError:
            pass
        _redis_subscriber_task = None

    # Cancel all running background tasks
    from services.task_manager import cancel_all_tasks
    await cancel_all_tasks()

    # Close Redis connection
    from config.redis_client import redis_client
    await redis_client.close()

    # Close MongoDB connection
    from config.database import mongodb_client
    await mongodb_client.close()

    # Close backend client
    await backend_client.close()


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8080,
        reload=True
    )
