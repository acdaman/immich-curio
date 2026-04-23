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

def _load_prompt() -> str:
    cfg = get_config()
    with open(cfg.scoring_prompt_path) as f:
        return f.read().strip()


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

    prompt = _load_prompt()
    contents = [
        types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
        prompt,
    ]

    try:
        result = await _call_gemini(client, cfg, contents)
        return result

    except (json.JSONDecodeError, ValueError):
        # Retry once with an explicit JSON reminder
        retry_contents = [
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            prompt + "\n\nIMPORTANT: Respond with ONLY valid JSON. No markdown. No explanation.",
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


async def analyze_decisions(approved_images: list[bytes], rejected_images: list[bytes]) -> str:
    """Send approved and rejected photo samples to Gemini for pattern analysis.

    Returns Gemini's plain-text analysis of what distinguishes approved from rejected photos.
    """
    cfg = get_config()
    client = genai.Client(api_key=cfg.gemini_api_key)

    contents: list = []
    if approved_images:
        contents.append(f"The following {len(approved_images)} photo(s) were APPROVED for printing:")
        for img in approved_images:
            contents.append(types.Part.from_bytes(data=img, mime_type="image/jpeg"))
    else:
        contents.append("No approved photos were available for this analysis.")

    if rejected_images:
        contents.append(f"The following {len(rejected_images)} photo(s) were REJECTED:")
        for img in rejected_images:
            contents.append(types.Part.from_bytes(data=img, mime_type="image/jpeg"))
    else:
        contents.append("No rejected photos were available for this analysis.")

    contents.append(
        "Analyze the curator's decisions. Answer these three questions concisely:\n"
        "1. What do the APPROVED photos have in common? (visual quality, composition, subject, mood)\n"
        "2. What do the REJECTED photos have in common?\n"
        "3. Any surprising or noteworthy observations about this curator's taste?\n\n"
        "Be specific. 3-5 bullet points per section. Plain text only, no markdown headers."
    )

    response = await asyncio.to_thread(
        client.models.generate_content,
        model=cfg.gemini_model,
        contents=contents,
    )
    return response.text.strip()
