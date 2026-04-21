"""Gemini Flash scoring wrapper with rate-limit handling (free tier: 5 RPM)."""

import asyncio
import json
import logging

from google import genai
from google.genai import types

from curio.config import get_config

logger = logging.getLogger(__name__)

RATE_LIMIT_SLEEP = 13    # seconds between successful calls (~4.6 RPM)
RATE_LIMIT_BACKOFF = 65  # seconds to wait after a 429 before retrying

SCORING_PROMPT = """You are evaluating a photo for physical printing on lustre paper for home wall display.
Score this photo strictly.

Respond with JSON only — no markdown, no explanation, just the JSON object:
{
  "score": "yes" | "maybe" | "no",
  "reason": "one sentence explanation"
}

Score "yes" if: sharp focus throughout, strong composition, meaningful or beautiful subject, clearly worth printing.
Score "maybe" if: decent photo with minor issues — slight blur, awkward crop, good subject but not exceptional.
Score "no" if: blurry, out of focus, duplicate/similar to many others, screenshot, document, text overlay, low light noise, not suitable for printing.

Target calibration: roughly 20% yes, 40% maybe, 40% no. Be strict — not every decent photo deserves to be printed."""


def _is_rate_limit(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "429" in msg or "resource_exhausted" in msg or "quota" in msg or "rate" in msg


def _parse_response(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    result = json.loads(text)
    if result.get("score") not in ("yes", "maybe", "no"):
        raise ValueError(f"Invalid score value: {result.get('score')!r}")
    return result


async def _call_gemini(client, cfg, contents) -> dict:
    """Call Gemini with up to 3 retries on 429, exponential backoff."""
    backoff = RATE_LIMIT_BACKOFF
    for attempt in range(3):
        try:
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=cfg.gemini_model,
                contents=contents,
            )
            return _parse_response(response.text)
        except (json.JSONDecodeError, ValueError) as e:
            # Bad JSON — not a rate limit, don't retry
            raise
        except Exception as e:
            if _is_rate_limit(e):
                logger.warning("Gemini 429 — sleeping %ds before retry (attempt %d/3)", backoff, attempt + 1)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
            else:
                raise
    raise RuntimeError("Gemini rate limit retries exhausted")


async def score_photo(image_bytes: bytes, asset_id: str) -> dict | None:
    """Score a photo. Returns {'score': 'yes'|'maybe'|'no', 'reason': str} or None on failure."""
    cfg = get_config()
    client = genai.Client(api_key=cfg.gemini_api_key)

    contents = [
        types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
        SCORING_PROMPT,
    ]

    try:
        result = await _call_gemini(client, cfg, contents)
        return result

    except (json.JSONDecodeError, ValueError):
        # Retry once with an explicit JSON reminder
        retry_contents = [
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            SCORING_PROMPT + "\n\nIMPORTANT: Respond with ONLY valid JSON. No markdown. No explanation.",
        ]
        try:
            await asyncio.sleep(RATE_LIMIT_SLEEP)
            return await _call_gemini(client, cfg, retry_contents)
        except Exception:
            return None

    except Exception as e:
        logger.warning("Gemini error for %s: %s", asset_id, e)
        return None

    finally:
        await asyncio.sleep(RATE_LIMIT_SLEEP)
