"""
Wingman AI — FastAPI Application with Redis Pub/Sub Integration.

A dating-practice roleplay chatbot where the LLM plays Indian girl characters
(Riya, Kavya, Tanya, Simran, Neha) in specific scenarios.
Subscribes to raasta:ai:request Redis channel, runs the roleplay LLM agent,
and publishes replies to raasta:ai:response.
"""
import asyncio
import logging
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from config.settings import settings
from services.socket_worker import start_socket_worker
from services.backend_client import backend_client
from app.routes.events import router as events_router
from app.routes.roleplay import router as roleplay_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

_socket_worker_task: Optional[asyncio.Task] = None

app = FastAPI(
    title="Wingman AI",
    description="AI roleplay chatbot — practice dating conversations with realistic characters",
    version="3.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://www.lovedr.in",
        "https://lovedr.in",
        "https://coach.lovedr.in",
        "https://www.raasta.today",
        "https://raasta.today",
        "https://stage-love-doc-ai.lovedr.in",
        "http://localhost:3001",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Strict-Transport-Security"] = (
            "max-age=16070400; includeSubDomains"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Content-Security-Policy"] = "default-src 'self'"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store, no-cache, max-age=0"
        response.headers["Server"] = ""
        return response


app.add_middleware(SecurityHeadersMiddleware)
app.include_router(events_router)
app.include_router(roleplay_router)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str
    timestamp: str
    openai_configured: bool
    backend_configured: bool
    redis_connected: bool
    mongodb_configured: bool
    postgres_connected: bool
    socket_worker_running: bool


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_model=dict)
async def root():
    return {
        "name": "Wingman AI",
        "version": "3.0.0",
        "description": "AI roleplay chatbot — practice dating conversations with realistic characters",
        "docs": "/docs",
    }


@app.get("/health", response_model=HealthResponse)
async def health_check():
    from config.redis_client import redis_client
    redis_healthy = await redis_client.health_check()

    from config.database import mongodb_client
    mongo_healthy = await mongodb_client.health_check()

    from config.pg_client import pg_client
    pg_healthy = await pg_client.health_check()

    return HealthResponse(
        status="healthy",
        timestamp=datetime.utcnow().isoformat(),
        openai_configured=bool(settings.OPENAI_API_KEY),
        backend_configured=bool(settings.BACKEND_BASE_URL),
        redis_connected=redis_healthy,
        mongodb_configured=mongo_healthy,
        postgres_connected=pg_healthy,
        socket_worker_running=_socket_worker_task is not None
        and not _socket_worker_task.done(),
    )


@app.get("/session")
async def get_session(user_uid: str, character_id: str, scenario_id: str):
    """
    Return (or create) the canonical session ID for a user + character + scenario triplet.

    Creates a new UUID v4 session the first time the combination is seen, then
    returns the same ID on every subsequent call until it expires (24 h TTL).
    """
    if not user_uid or not character_id or not scenario_id:
        raise HTTPException(
            status_code=422,
            detail="user_uid, character_id, and scenario_id are all required",
        )

    from services.webhook_handler import get_or_create_session_id
    session_id, created = await get_or_create_session_id(
        user_uid, character_id, scenario_id
    )
    return {
        "session_id": session_id,
        "user_uid": user_uid,
        "character_id": character_id,
        "scenario_id": scenario_id,
        "created": created,
    }


# ---------------------------------------------------------------------------
# Background watchdog
# ---------------------------------------------------------------------------

async def run_mongodb_watchdog():
    while True:
        await asyncio.sleep(60)
        from config.database import mongodb_client
        healthy = await mongodb_client.health_check()
        if not healthy:
            logger.warning("MongoDB watchdog: connection lost, attempting reconnect...")
            await mongodb_client.connect()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_event():
    logger.info("Starting Wingman AI with Socket.IO Worker...")
    logger.info(f"OpenAI Model: {settings.OPENAI_MODEL}")
    logger.info(f"Backend URL: {settings.BACKEND_BASE_URL}")
    logger.info(f"Socket Server: {settings.SOCKET_SERVER_URL or '(not set)'}")

    from config.redis_client import redis_client
    logger.info(f"Connecting to Redis: {settings.REDIS_URL}")
    redis_connected = await redis_client.connect()
    if redis_connected:
        logger.info(f"Redis connected (task TTL: {settings.REDIS_TASK_TTL}s)")
    else:
        logger.warning("Running without Redis (in-memory task tracking only)")

    from config.database import mongodb_client
    logger.info(f"Connecting to MongoDB: {settings.MONGODB_DATABASE}")
    mongo_connected = await mongodb_client.connect()
    if mongo_connected:
        logger.info("MongoDB connected successfully")
    else:
        logger.warning("MongoDB connection failed")

    from config.pg_client import pg_client
    if settings.DATABASE_URL:
        logger.info("Connecting to Postgres...")
        pg_connected = await pg_client.connect()
        if pg_connected:
            logger.info("Postgres connected successfully")
        else:
            logger.warning("Postgres connection failed — direct DB queries will be unavailable")
    else:
        logger.warning("DATABASE_URL not set — Postgres client disabled")

    asyncio.create_task(run_mongodb_watchdog())

    global _socket_worker_task
    _socket_worker_task = asyncio.create_task(start_socket_worker())
    logger.info("Socket.IO worker task started")


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down Wingman AI...")

    global _socket_worker_task
    if _socket_worker_task is not None:
        _socket_worker_task.cancel()
        try:
            await _socket_worker_task
        except asyncio.CancelledError:
            pass
        _socket_worker_task = None

    from services.task_manager import cancel_all_tasks
    await cancel_all_tasks()

    from config.redis_client import redis_client
    await redis_client.close()

    from config.pg_client import pg_client
    await pg_client.close()

    from config.database import mongodb_client
    await mongodb_client.close()

    await backend_client.close()


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.PORT,
        reload=True,
    )
