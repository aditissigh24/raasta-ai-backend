"""
Eval chain — Claude Haiku for engagement scoring.

Non-streaming (background only). Returns structured JSON.
Uses Anthropic prefix caching on the eval system prompt.
"""
import json
import logging
from typing import Optional

import anthropic

from config.settings import settings

logger = logging.getLogger(__name__)

_anthropic_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic(
            api_key=settings.ANTHROPIC_API_KEY,
        )
    return _anthropic_client


async def run_eval(
    eval_system_prompt: str,
    user_messages_text: str,
) -> Optional[dict]:
    """
    Score user engagement using Claude Haiku.

    Returns dict with keys: score (float), reason (str), suggested_hook (str|None).
    Returns None on failure.
    """
    client = _get_client()

    try:
        logger.info("eval_chain: Claude Haiku engagement eval → start")
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            temperature=0,
            system=[
                {
                    "type": "text",
                    "text": eval_system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": f"Messages to evaluate:\n{user_messages_text}",
                }
            ],
        )

        raw_text = response.content[0].text.strip()

        # Strip markdown code fences if present
        if raw_text.startswith("```"):
            lines = raw_text.split("\n")
            raw_text = "\n".join(lines[1:-1]) if len(lines) > 2 else raw_text

        result = json.loads(raw_text)
        score = float(result.get("score", 3.0))
        reason = str(result.get("reason", ""))
        logger.info(
            f"eval_chain: Claude Haiku engagement eval → done "
            f"score={score:.1f} reason='{reason[:80]}'"
        )
        return {
            "score":          score,
            "reason":         reason,
            "suggested_hook": result.get("suggested_hook"),
        }

    except json.JSONDecodeError as e:
        logger.warning(f"eval_chain: JSON parse error: {e}. Raw: {raw_text!r}")
        return None
    except Exception as e:
        logger.warning(f"eval_chain: failed: {e}")
        return None
