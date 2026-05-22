"""Gemini scoring via the Batch API (50% cost discount, asynchronous)."""

import asyncio
import json
import logging

from google import genai
from google.genai import types

from curio.config import get_config

logger = logging.getLogger(__name__)

RATE_LIMIT_BACKOFF = 65  # seconds before retry after a 429 on job submission

_TERMINAL_STATES = frozenset({
    "JOB_STATE_SUCCEEDED",
    "JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED",
    "JOB_STATE_EXPIRED",
})
SUCCESS_STATE = "JOB_STATE_SUCCEEDED"


def _load_prompt() -> str:
    cfg = get_config()
    with open(cfg.scoring_prompt_path) as f:
        return f.read().strip()


def _load_group_prompt() -> str:
    cfg = get_config()
    with open(cfg.group_scoring_prompt_path) as f:
        return f.read().strip()


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
        raise ValueError("Missing or invalid 'scores' dict in response")
    missing = expected_ids - set(scores.keys())
    if missing:
        raise ValueError(f"Gemini response missing asset IDs: {missing}")
    for asset_id, entry in scores.items():
        if entry.get("score") not in ("yes", "no"):
            raise ValueError(f"Invalid score for {asset_id!r}: {entry.get('score')!r}")
    return {k: v for k, v in scores.items() if k in expected_ids}


def _make_client() -> genai.Client:
    return genai.Client(api_key=get_config().gemini_api_key)


def _build_single_request(image_bytes: bytes, prompt: str) -> types.InlinedRequest:
    return types.InlinedRequest(
        contents=[
            types.Content(parts=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                types.Part.from_text(text=prompt),
            ])
        ]
    )


def _build_group_request(
    images_with_ids: list[tuple[bytes, str, bool]],
    prompt: str,
) -> types.InlinedRequest:
    parts: list[types.Part] = [
        types.Part.from_text(
            text=(
                f"You are scoring a sequence of {len(images_with_ids)} photos taken within a short time window. "
                "Use exactly these asset IDs in your response (no others):"
            )
        )
    ]
    for i, (image_bytes, asset_id, is_favorite) in enumerate(images_with_ids, 1):
        fav_marker = " [FAVOURITE]" if is_favorite else ""
        parts.append(types.Part.from_text(text=f"Photo {i} — asset ID: {asset_id}{fav_marker}"))
        parts.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))
    parts.append(types.Part.from_text(text=prompt))
    return types.InlinedRequest(
        contents=[types.Content(parts=parts)]
    )


async def _submit_batch(request: types.InlinedRequest) -> str:
    """Submit a single-request batch job. Returns the job_id segment of the job name."""
    cfg = get_config()
    client = _make_client()
    backoff = RATE_LIMIT_BACKOFF
    for attempt in range(3):
        try:
            batch = await asyncio.to_thread(
                client.batches.create,
                model=cfg.gemini_model,
                src=[request],
            )
            return batch.name.split("/")[-1]
        except Exception as e:
            msg = str(e).lower()
            if "429" in msg or "resource_exhausted" in msg or "quota" in msg or "rate" in msg:
                logger.warning("Batch submit 429 — sleeping %ds (attempt %d/3)", backoff, attempt + 1)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
            else:
                raise
    raise RuntimeError("Batch job submission rate limit retries exhausted")


async def submit_single_batch_job(image_bytes: bytes, asset_id: str) -> str:
    """Submit a single-photo batch scoring job. Returns job_id."""
    job_id = await _submit_batch(_build_single_request(image_bytes, _load_prompt()))
    logger.info("Submitted single batch job %s for asset %s", job_id, asset_id)
    return job_id


async def submit_group_batch_job(images_with_ids: list[tuple[bytes, str, bool]]) -> str:
    """Submit a burst-group batch scoring job. Returns job_id."""
    asset_ids = [aid for _, aid, _ in images_with_ids]
    job_id = await _submit_batch(_build_group_request(images_with_ids, _load_group_prompt()))
    logger.info("Submitted group batch job %s for assets %s", job_id, asset_ids)
    return job_id


def _state_name(state) -> str:
    """Normalise state to a plain string regardless of whether it's an enum or str."""
    return state.name if hasattr(state, "name") else str(state)


def poll_batch_job(job_id: str) -> str:
    """Return current state string of a batch job (blocking, run in thread)."""
    client = _make_client()
    batch = client.batches.get(name=f"batches/{job_id}")
    return _state_name(batch.state)


def get_batch_result(job_id: str) -> str | None:
    """Return the response text from a completed single-request batch job (blocking)."""
    client = _make_client()
    batch = client.batches.get(name=f"batches/{job_id}")
    try:
        responses = batch.dest.inlined_responses
        if not responses:
            return None
        return responses[0].response.text
    except AttributeError as e:
        logger.error("Unexpected batch response structure for job %s: %s", job_id, e)
        return None


def is_terminal(state: str) -> bool:
    return state in _TERMINAL_STATES


def parse_batch_result(result_text: str, asset_ids: list[str]) -> dict[str, dict] | None:
    """Parse batch response text into {asset_id: {"score": ..., "reason": ...}}.

    Tries group format first (multiple asset IDs), falls back to single-photo format.
    Returns None if parsing fails entirely.
    """
    try:
        parsed = _parse_group_response(result_text, set(asset_ids))
        return parsed
    except Exception:
        pass

    try:
        result = _parse_response(result_text)
        return {aid: result for aid in asset_ids}
    except Exception as e:
        logger.warning("Failed to parse batch result: %s", e)
        return None


async def analyze_decisions(approved_images: list[bytes], rejected_images: list[bytes]) -> str:
    """Send approved and rejected photo samples to Gemini for pattern analysis.

    Runs synchronously (interactive, latency-sensitive — not batched).
    """
    cfg = get_config()
    client = _make_client()

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
