"""Gemini Flash scoring wrapper with 13s rate-limit sleep (free tier: 5 RPM)."""

import asyncio
import json

from google import genai
from google.genai import types

from curio.config import get_config

RATE_LIMIT_SLEEP = 13  # seconds between calls to stay under 5 RPM

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


def _parse_response(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    result = json.loads(text)
    if result.get("score") not in ("yes", "maybe", "no"):
        raise ValueError(f"Invalid score value: {result.get('score')!r}")
    return result


async def score_photo(image_bytes: bytes, asset_id: str) -> dict | None:
    """Score a photo. Returns {'score': 'yes'|'maybe'|'no', 'reason': str} or None on failure."""
    cfg = get_config()
    client = genai.Client(api_key=cfg.gemini_api_key)

    contents = [
        types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
        SCORING_PROMPT,
    ]

    try:
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=cfg.gemini_model,
            contents=contents,
        )
        return _parse_response(response.text)

    except (json.JSONDecodeError, ValueError):
        # Retry once with an explicit JSON reminder
        retry_contents = [
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            SCORING_PROMPT + "\n\nIMPORTANT: Respond with ONLY valid JSON. No markdown. No explanation.",
        ]
        try:
            await asyncio.sleep(RATE_LIMIT_SLEEP)
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=cfg.gemini_model,
                contents=retry_contents,
            )
            return _parse_response(response.text)
        except Exception:
            return None

    except Exception:
        return None

    finally:
        await asyncio.sleep(RATE_LIMIT_SLEEP)
