"""Async Immich REST API client."""

import httpx

from curio.config import get_config

PRINT_ALBUM_ID = "b2f416c7-4f72-4c4c-a138-c7b23978eabc"


def _client() -> httpx.AsyncClient:
    cfg = get_config()
    return httpx.AsyncClient(
        base_url=cfg.immich_base_url,
        headers={"x-api-key": cfg.immich_api_key, "Accept": "application/json"},
        timeout=30,
    )


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
    """Create tag if needed, return its ID. Falls back to GET if tag already exists."""
    resp = await client.post("/api/tags", json={"name": tag_path})
    if resp.status_code in (200, 201):
        return resp.json()["id"]
    # 400 or 409 means the tag already exists — find it by value
    if resp.status_code in (400, 409):
        tags_resp = await client.get("/api/tags")
        tags_resp.raise_for_status()
        for tag in tags_resp.json():
            if tag.get("value") == tag_path:
                return tag["id"]
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
            f"/api/albums/{PRINT_ALBUM_ID}/assets", json={"ids": [asset_id]}
        )
        resp.raise_for_status()


async def mark_favorite(asset_id: str) -> None:
    cfg = get_config()
    if cfg.dry_run:
        print(f"[DRY RUN] mark_favorite({asset_id!r})")
        return

    async with _client() as client:
        resp = await client.put("/api/assets", json={"ids": [asset_id], "isFavorite": True})
        resp.raise_for_status()
