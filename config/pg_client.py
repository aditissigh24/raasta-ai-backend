"""
Direct asyncpg PostgreSQL client.

Replaces backend HTTP calls for reading scenario/character/user data and
writing session summaries. Column names follow the Prisma schema conventions
(camelCase, quoted where needed).
"""
import asyncio
import logging
import uuid
from typing import Optional

import asyncpg

from config.settings import settings

logger = logging.getLogger(__name__)


class PGClient:
    """Async PostgreSQL client backed by an asyncpg connection pool."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        if not self._dsn:
            logger.warning("DATABASE_URL not set — Postgres client disabled")
            return False
        try:
            self._pool = await asyncpg.create_pool(
                dsn=self._dsn,
                min_size=1,
                max_size=10,
                command_timeout=30,
                statement_cache_size=0,
            )
            # Smoke-test
            async with self._pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            logger.info("Postgres connection pool established")
            return True
        except Exception as e:
            logger.error(f"Postgres connection failed: {e}", exc_info=True)
            self._pool = None
            return False

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("Postgres connection pool closed")

    async def health_check(self) -> bool:
        if not self._pool:
            return False
        try:
            async with self._pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:
            return False

    @property
    def is_available(self) -> bool:
        return self._pool is not None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _record_to_dict(self, record: asyncpg.Record) -> dict:
        return dict(record)

    def _records_to_list(self, records: list) -> list[dict]:
        return [dict(r) for r in records]

    # ------------------------------------------------------------------
    # User
    # ------------------------------------------------------------------

    async def fetch_user(self, user_id: str) -> Optional[dict]:
        """Return a User row as a dict, or None if not found / pool unavailable."""
        if not self._pool:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT
                        id,
                        name,
                        "ageRange",
                        email,
                        phone,
                        "countryCode",
                        gender
                    FROM "User"
                    WHERE id = $1
                    """,
                    user_id,
                )
            if not row:
                return None
            data = self._record_to_dict(row)
            # Provide age as numeric for legacy code that reads user_config["age"]
            age_range = data.get("ageRange") or ""
            data["age"] = _parse_age_range(age_range)
            data["user_id"] = data["id"]
            return data
        except Exception as e:
            logger.warning(f"fetch_user({user_id}) failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Character
    # ------------------------------------------------------------------

    async def fetch_character(self, character_id: str) -> Optional[dict]:
        """Return a Character row as a dict."""
        if not self._pool:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    'SELECT * FROM "Character" WHERE id = $1',
                    character_id,
                )
            if not row:
                return None
            data = self._record_to_dict(row)
            # Normalise to the field names the roleplay prompt expects
            data.setdefault("char_id", data.get("id", character_id))
            return data
        except Exception as e:
            logger.warning(f"fetch_character({character_id}) failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Scenario
    # ------------------------------------------------------------------

    async def fetch_scenario(self, scenario_id: str) -> Optional[dict]:
        """Return a Scenario row joined with its Chapter."""
        if not self._pool:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT
                        s.*,
                        c.name AS "chapterName"
                    FROM "Scenario" s
                    LEFT JOIN "Chapter" c ON s."chapterId" = c.id
                    WHERE s.id = $1
                    """,
                    scenario_id,
                )
            if not row:
                return None
            data = self._record_to_dict(row)
            data.setdefault("scenario_id", data.get("id", scenario_id))
            return data
        except Exception as e:
            logger.warning(f"fetch_scenario({scenario_id}) failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Chapters
    # ------------------------------------------------------------------

    async def fetch_chapters(self) -> list[dict]:
        """Return all Chapter rows."""
        if not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    'SELECT id, name, description FROM "Chapter" ORDER BY id'
                )
            return self._records_to_list(rows)
        except Exception as e:
            logger.warning(f"fetch_chapters() failed: {e}")
            return []

    async def fetch_scenarios_by_chapter(self, chapter_id: str) -> list[dict]:
        """Return Scenario rows belonging to a Chapter, with character info."""
        if not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        s.id            AS scenario_id,
                        s."characterId" AS char_id,
                        s.title         AS scenario_title,
                        s.difficulty,
                        s."situationSetupForUser" AS situation_setup_for_user,
                        s."learningObjective"     AS learning_objective
                    FROM "Scenario" s
                    WHERE s."chapterId" = $1
                    ORDER BY s.id
                    """,
                    chapter_id,
                )
            return self._records_to_list(rows)
        except Exception as e:
            logger.warning(f"fetch_scenarios_by_chapter({chapter_id}) failed: {e}")
            return []

    # ------------------------------------------------------------------
    # Conversation messages (roleplay history)
    # ------------------------------------------------------------------

    async def fetch_messages_by_conversation(self, conversation_id: str) -> list[dict]:
        """
        Return all messages for a conversation ordered by creation time.

        Each dict has at minimum: text, senderType, senderId, createdAt.
        """
        if not self._pool or not conversation_id:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        id,
                        text,
                        "senderType",
                        "senderId",
                        "createdAt"
                    FROM "Message"
                    WHERE "conversationId" = $1
                    ORDER BY "createdAt" ASC
                    """,
                    conversation_id,
                )
            return self._records_to_list(rows)
        except Exception as e:
            logger.warning(f"fetch_messages_by_conversation({conversation_id}) failed: {e}")
            return []

    # ------------------------------------------------------------------
    # Session summaries (for continuity context)
    # ------------------------------------------------------------------

    async def fetch_recent_session_summaries(
        self,
        user_id: str,
        conversation_id: str,
        limit: int = 3,
    ) -> list[dict]:
        """Return recent session summaries for a user, excluding the current conversation."""
        if not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        id,
                        "summaryText",
                        "createdAt"
                    FROM "SessionSummary"
                    WHERE "userId" = $1
                      AND "conversationId" != $2
                    ORDER BY "createdAt" DESC
                    LIMIT $3
                    """,
                    user_id,
                    conversation_id or "",
                    limit,
                )
            return self._records_to_list(rows)
        except Exception as e:
            logger.warning(f"fetch_recent_session_summaries({user_id}) failed: {e}")
            return []

    async def create_session_summary(
        self,
        user_id: str,
        conversation_id: str,
        session_id: str,
        summary_text: str,
        character_id: Optional[str] = None,
        scenario_id: str = "",
    ) -> bool:
        """Insert a session summary row. Returns True on success."""
        if not self._pool:
            return False
        try:
            row_id = str(uuid.uuid4())
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO "SessionSummary"
                        ("id", "userId", "conversationId", "sessionId", "summaryText", "characterId", "scenarioId", "createdAt")
                    VALUES
                        ($1, $2, $3, $4, $5, $6, $7, NOW())
                    ON CONFLICT DO NOTHING
                    """,
                    row_id,
                    user_id,
                    conversation_id,
                    session_id,
                    summary_text,
                    character_id,
                    scenario_id,
                )
            logger.info(f"Session summary stored for user={user_id} conv={conversation_id}")
            return True
        except Exception as e:
            logger.error(f"create_session_summary failed: {e}", exc_info=True)
            return False


# ------------------------------------------------------------------
# Helper
# ------------------------------------------------------------------

def _parse_age_range(age_range: str) -> int:
    """Convert an ageRange string like '22-25' to an approximate integer."""
    if not age_range:
        return 25
    try:
        parts = age_range.split("-")
        return int(parts[0])
    except (ValueError, IndexError):
        return 25


# ------------------------------------------------------------------
# Singleton
# ------------------------------------------------------------------

pg_client = PGClient(dsn=settings.DATABASE_URL)
