"""Async Immich REST API client."""

import httpx

from curio.config import get_config

# tag value → tag ID, populated lazily and updated on creation
_tag_cache: dict[str, str] = {}


def _client() -> httpx.AsyncClient:
    cfg = get_config()
    return httpx.AsyncClient(
        base_url=cfg.immich_base_url,
        headers={"x-api-key": cfg.immich_api_key, "Accept": "application/json"},
        timeout=30,
    )


async def _load_tag_cache(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/tags")
    resp.raise_for_status()
    for tag in resp.json():
        _tag_cache[tag["value"]] = tag["id"]


async def get_asset_info(asset_id: str) -> dict:
    async with _client() as client:
        resp = await client.get(f"/api/assets/{asset_id}")
        resp.raise_for_status()
        return resp.json()


async def get_thumbnail(asset_id: str) -> bytes:
    async with _client() as client:
        resp = await client.get(f"/api/assets/{asset_id}/thumbnail", params={"size": "preview"})
        resp.raise_for_status()
        return resp.content


async def _ensure_tag(client: httpx.AsyncClient, tag_path: str) -> str:
    """Return tag ID from cache, creating the tag if it doesn't exist yet."""
    if tag_path in _tag_cache:
        return _tag_cache[tag_path]

    # Not cached — try to create it
    resp = await client.post("/api/tags", json={"name": tag_path})
    if resp.status_code in (200, 201):
        tag_id = resp.json()["id"]
        _tag_cache[tag_path] = tag_id
        return tag_id

    # Already exists (400/409) — load all tags into cache and retry
    if resp.status_code in (400, 409):
        await _load_tag_cache(client)
        if tag_path in _tag_cache:
            return _tag_cache[tag_path]

    resp.raise_for_status()
    raise RuntimeError(f"Could not find or create tag {tag_path!r}")


async def apply_tag(asset_id: str, tag_path: str) -> None:
    cfg = get_config()
    if cfg.dry_run:
        print(f"[DRY RUN] apply_tag({asset_id!r}, {tag_path!r})")
        return

    async with _client() as client:
        tag_id = await _ensure_tag(client, tag_path)
        resp = await client.put(f"/api/tags/{tag_id}/assets", json={"ids": [asset_id]})
        resp.raise_for_status()


async def remove_tag(asset_id: str, tag_path: str) -> None:
    cfg = get_config()
    if cfg.dry_run:
        print(f"[DRY RUN] remove_tag({asset_id!r}, {tag_path!r})")
        return

    async with _client() as client:
        tag_id = await _ensure_tag(client, tag_path)
        resp = await client.request(
            "DELETE", f"/api/tags/{tag_id}/assets", json={"ids": [asset_id]}
        )
        resp.raise_for_status()


async def add_to_print_album(asset_id: str) -> None:
    cfg = get_config()
    if cfg.dry_run:
        print(f"[DRY RUN] add_to_print_album({asset_id!r})")
        return

    async with _client() as client:
        resp = await client.put(
            f"/api/albums/{cfg.print_album_id}/assets", json={"ids": [asset_id]}
        )
        resp.raise_for_status()


async def download_original(asset_id: str) -> tuple[bytes, str]:
    """Return (file_bytes, original_filename) for the asset at full quality."""
    async with _client() as client:
        info_resp = await client.get(f"/api/assets/{asset_id}")
        info_resp.raise_for_status()
        info = info_resp.json()
        filename = info.get("originalFileName", f"{asset_id}.jpg")

        dl_resp = await client.get(
            f"/api/assets/{asset_id}/original",
            headers={"Accept": "*/*"},
            timeout=120.0,
        )
        dl_resp.raise_for_status()
        return dl_resp.content, filename


async def mark_favorite(asset_id: str) -> None:
    cfg = get_config()
    if cfg.dry_run:
        print(f"[DRY RUN] mark_favorite({asset_id!r})")
        return

    async with _client() as client:
        resp = await client.put("/api/assets", json={"ids": [asset_id], "isFavorite": True})
        resp.raise_for_status()
