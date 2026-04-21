"""
Langfuse prompt loader with Redis caching and hardcoded fallback.

get(prompt_name)  — returns a CachedTemplate with a .compile(variables) method.

Resolution order:
  1. Redis cache  (TTL: 10 min, refreshed on every hit)
  2. Langfuse API (cached to Redis on fetch)
  3. Hardcoded fallback (if Langfuse is down and cache is cold)
"""
import json
import logging
from typing import Optional

from config.redis_client import redis_client
from config.settings import settings
from wingman.prompts.fallback_templates import FALLBACK_TEMPLATES

logger = logging.getLogger(__name__)

TEMPLATE_CACHE_TTL = 600  # 10 minutes
_CACHE_PREFIX = "langfuse:template:"

# Lazily initialised Langfuse client
_langfuse = None


def _get_langfuse():
    global _langfuse
    if _langfuse is None and settings.LANGFUSE_SECRET_KEY:
        try:
            from langfuse import Langfuse
            _langfuse = Langfuse(
                secret_key=settings.LANGFUSE_SECRET_KEY,
                public_key=settings.LANGFUSE_PUBLIC_KEY,
                host=settings.LANGFUSE_HOST,
            )
        except Exception as e:
            logger.warning(f"Langfuse init failed: {e}")
    return _langfuse


class CachedTemplate:
    """
    Thin wrapper around a raw template string.
    .compile(variables) replaces {placeholder} patterns with values.
    """

    def __init__(self, text: str, source: str = "unknown"):
        self._text = text
        self._source = source

    def compile(self, variables: dict) -> str:
        result = self._text
        for key, value in variables.items():
            placeholder = "{" + key + "}"
            result = result.replace(placeholder, str(value) if value is not None else "")
        return result

    @property
    def text(self) -> str:
        return self._text

    def __repr__(self) -> str:
        return f"CachedTemplate(source={self._source!r}, length={len(self._text)})"


async def get(prompt_name: str) -> CachedTemplate:
    """
    Fetch a prompt template by name.

    Always returns a usable CachedTemplate — never raises.
    On Langfuse outage, falls back to the hardcoded template.
    """
    # 1. Try Redis cache
    # cached_text = await _get_from_redis(prompt_name)
    # if cached_text is not None:
    #     logger.info(f"langfuse_loader: '{prompt_name}' → Redis cache hit")
    #     return CachedTemplate(cached_text, source="redis")

    # 2. Try Langfuse
    langfuse_text = await _get_from_langfuse(prompt_name)
    if langfuse_text is not None:
        logger.info(f"langfuse_loader: '{prompt_name}' → fetched from Langfuse API, caching to Redis")
        # await _store_in_redis(prompt_name, langfuse_text)
        return CachedTemplate(langfuse_text, source="langfuse")

    # 3. Hardcoded fallback
    fallback = FALLBACK_TEMPLATES.get(prompt_name)
    if fallback:
        logger.warning(
            f"langfuse_loader: '{prompt_name}' → using hardcoded fallback "
            "(Langfuse unavailable and Redis cache cold)"
        )
        return CachedTemplate(fallback, source="fallback")

    # Final safety net — empty template, conversations won't crash
    logger.error(f"langfuse_loader: '{prompt_name}' → NO template found, using empty fallback")
    return CachedTemplate("", source="missing")


async def _get_from_redis(prompt_name: str) -> Optional[str]:
    if not redis_client.is_available:
        return None
    try:
        raw = await redis_client.get(f"{_CACHE_PREFIX}{prompt_name}")
        if raw:
            data = json.loads(raw)
            # Refresh TTL on hit so hot templates stay warm
            await redis_client.set(
                f"{_CACHE_PREFIX}{prompt_name}", raw, ex=TEMPLATE_CACHE_TTL
            )
            return data.get("text", "")
    except Exception as e:
        logger.debug(f"Redis template fetch failed for '{prompt_name}': {e}")
    return None


async def _get_from_langfuse(prompt_name: str) -> Optional[str]:
    lf = _get_langfuse()
    if not lf:
        return None
    try:
        # Run blocking Langfuse call in a thread to not block the event loop
        import asyncio
        prompt = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: lf.get_prompt(prompt_name, label="production"),
        )
        # Langfuse text prompt: .prompt is the raw text string
        if hasattr(prompt, "prompt"):
            return str(prompt.prompt)
        # Chat prompt: join all messages into a single text
        if hasattr(prompt, "get_langchain_prompt"):
            messages = prompt.get_langchain_prompt()
            return "\n\n".join(
                m.content if hasattr(m, "content") else str(m)
                for m in messages
            )
    except Exception as e:
        logger.warning(f"Langfuse fetch failed for '{prompt_name}': {e}")
    return None


async def _store_in_redis(prompt_name: str, text: str) -> None:
    if not redis_client.is_available:
        return
    try:
        payload = json.dumps({"text": text})
        await redis_client.set(
            f"{_CACHE_PREFIX}{prompt_name}", payload, ex=TEMPLATE_CACHE_TTL
        )
    except Exception as e:
        logger.debug(f"Redis template store failed for '{prompt_name}': {e}")
