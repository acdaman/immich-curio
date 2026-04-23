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


def _load_group_prompt() -> str:
    cfg = get_config()
    with open(cfg.group_scoring_prompt_path) as f:
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


def _parse_group_response(text: str, expected_ids: set[str]) -> dict[str, dict]:
    """Parse group scoring response. Raises ValueError on structural problems."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    result = json.loads(text)
    scores = result.get("scores")
    if not isinstance(scores, dict):
        raise ValueError(f"Missing or invalid 'scores' dict in response")
    missing = expected_ids - set(scores.keys())
    if missing:
        raise ValueError(f"Gemini response missing asset IDs: {missing}")
    for asset_id, entry in scores.items():
        if entry.get("score") not in ("yes", "no"):
            raise ValueError(f"Invalid score for {asset_id!r}: {entry.get('score')!r}")
    return {k: v for k, v in scores.items() if k in expected_ids}


async def _call_gemini_raw(client, cfg, contents) -> str:
    """Call Gemini with up to 3 retries on 429. Returns raw response text."""
    backoff = RATE_LIMIT_BACKOFF
    for attempt in range(3):
        try:
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=cfg.gemini_model,
                contents=contents,
            )
            return response.text
        except Exception as e:
            if _is_rate_limit(e):
                logger.warning("Gemini 429 — sleeping %ds before retry (attempt %d/3)", backoff, attempt + 1)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
            else:
                raise
    raise RuntimeError("Gemini rate limit retries exhausted")


async def _call_gemini(client, cfg, contents) -> dict:
    """Call Gemini with up to 3 retries on 429, exponential backoff."""
    raw = await _call_gemini_raw(client, cfg, contents)
    return _parse_response(raw)


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


async def score_photo_group(
    images_with_ids: list[tuple[bytes, str, bool]],
) -> dict[str, dict] | None:
    """Score a sequence of related photos as a group.

    Returns {asset_id: {"score": "yes"|"no", "reason": str}} for every ID provided,
    or None on unrecoverable failure.

    images_with_ids: list of (jpeg_bytes, asset_id, is_favorite), chronological order.
    """
    if not images_with_ids:
        return None

    cfg = get_config()
    client = genai.Client(api_key=cfg.gemini_api_key)
    prompt = _load_group_prompt()
    expected_ids = {asset_id for _, asset_id, _ in images_with_ids}

    contents: list = [
        f"You are scoring a sequence of {len(images_with_ids)} photos taken within a short time window. "
        f"Use exactly these asset IDs in your response (no others):"
    ]
    for i, (image_bytes, asset_id, is_favorite) in enumerate(images_with_ids, 1):
        fav_marker = " [FAVOURITE]" if is_favorite else ""
        contents.append(f"Photo {i} — asset ID: {asset_id}{fav_marker}")
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))
    contents.append(prompt)

    async def _attempt(extra: str = "") -> dict[str, dict]:
        c = contents if not extra else contents + [extra]
        raw = await _call_gemini_raw(client, cfg, c)
        return _parse_group_response(raw, expected_ids)

    try:
        return await _attempt()

    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Group scoring parse error (%s) — retrying with JSON reminder", e)
        try:
            await asyncio.sleep(RATE_LIMIT_SLEEP)
            return await _attempt(
                "\n\nIMPORTANT: Respond with ONLY valid JSON. "
                "Every asset ID listed above MUST appear in 'scores'. Do not invent IDs."
            )
        except Exception as e2:
            logger.warning("Group scoring retry failed: %s", e2)
            return None

    except Exception as e:
        logger.warning("Gemini group scoring error: %s", e)
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
