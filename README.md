# Curio

> A personal project. It runs in my home lab to help me curate my family photo library for printing — feel free to adapt it.

I have ~16,000 family photos in [Immich](https://immich.app). Picking the best ones to print by hand would take forever. Curio automates the first pass: Gemini scores each photo in the background, and the best candidates are delivered one at a time to a Telegram bot where I approve, like, or reject with a single tap.

## How it works

Two async coroutines share one event loop:

1. **Queue filler** — fetches unscored photos from Immich and submits them to the [Gemini Batch API](https://ai.google.dev/gemini-api/docs/batch-api) for scoring at 50% of standard cost. Groups of burst photos (taken within 60 s on the same camera) are scored together so Gemini picks the best frame from each moment. Results are applied within minutes; in-flight jobs are tracked via Immich tags (`print/scored/batch/*`) so state survives restarts. Maintains a pool of ~40 reviewed candidates.
2. **Bot responder** — listens for Telegram button callbacks and slash commands. Each photo is delivered with three buttons: **Approve** (adds to Print album, queues for export), **Like** (marks as favourite in Immich without printing), or **Reject** (permanent). Sends the next photo after each decision. Commands: `/start` delivers the next queued photo, `/stats` shows a full pipeline and calibration breakdown, `/feedback` sends a sample of recent approved and rejected photos to Gemini and returns an analysis of your taste.

**Tag taxonomy:**

| Tag | Written by | Meaning |
|---|---|---|
| `print/scored/batch/{id}` | Queue filler | Submitted to Gemini batch job, awaiting result |
| `print/scored/yes` | Queue filler | Gemini approved — in queue |
| `print/scored/maybe` | Queue filler | Gemini uncertain — in queue |
| `print/scored/no` | Queue filler | Gemini rejected (single photo) |
| `print/scored/no/group` | Queue filler | Gemini rejected (lost to a better burst frame) |
| `print/scored/no/duplicate` | Queue filler | Discarded as Immich-flagged duplicate |
| `print/scored/yes/auto` | Queue filler | Auto-approved (Immich favourite) |
| `print/telegram/sent` | Bot | Photo sent to Telegram, awaiting decision |
| `print/rejected` | Bot | Rejected — permanent |
| `print/queued` | Bot | Approved, added to Print album |
| `print/exported` | Exporter | Full-res written to export folder |

## Compatibility

Known to work on **Immich v2.7.4**. Curio validates the database schema on every startup and will exit with an error if it doesn't match expectations, so incompatible versions fail loudly rather than silently misbehaving.

## Prerequisites

- **Immich** — self-hosted, with:
  - An API key (Settings → API Keys)
  - Your user UUID (Settings → Account)
  - A "Print" album (create it, then find its ID via `GET /api/albums`)
  - A read-only Postgres user (see [Database setup](#database-setup) below)
- **Gemini API key** — [Google AI Studio](https://aistudio.google.com/apikey) — **paid tier required** for Batch API access
- **Telegram bot** — create one with [@BotFather](https://t.me/botfather), then get your chat ID from [@userinfobot](https://t.me/userinfobot)
- **Docker + Docker Compose**

## Quick start

```bash
git clone https://github.com/acdaman/curio.git
cd curio
cp .env.example .env
# Fill in all values in .env
docker compose up -d
docker compose logs -f
```

The bot will send you a photo once the first batch of scoring jobs completes (typically within a few minutes of startup).

## Configuration

All configuration is via environment variables in `.env`. See `.env.example` for the full list with comments.

| Variable | Required | Default | Description |
|---|---|---|---|
| `IMMICH_URL` | Yes | — | Immich base URL (e.g. `http://immich_server:2283`) |
| `IMMICH_PUBLIC_URL` | No | — | External URL for links in captions (e.g. `https://photos.example.com`) |
| `IMMICH_API_KEY` | Yes | — | Immich API key |
| `IMMICH_USER_ID` | Yes | — | Your Immich user UUID |
| `PRINT_ALBUM_ID` | Yes | — | UUID of your "Print" album |
| `DB_HOST` | Yes | `postgres_db` | Postgres hostname |
| `DB_PORT` | No | `5432` | Postgres port |
| `DB_NAME` | No | `immich` | Database name |
| `DB_USER` | No | `immich_reader` | Postgres user |
| `DB_PASSWORD` | Yes | — | Postgres password |
| `GEMINI_API_KEY` | Yes | — | Google Gemini API key (paid tier required) |
| `GEMINI_MODEL` | No | `gemini-3.5-flash` | Gemini model name |
| `TELEGRAM_BOT_TOKEN` | Yes | — | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Yes | — | Your Telegram chat ID |
| `QUEUE_TARGET_SIZE` | No | `40` | Scored candidates to keep in queue |
| `QUEUE_BATCH_TRIGGER` | No | `20` | Submit new batch jobs when queue drops to this level |
| `EXPORT_ENABLED` | No | `false` | Enable full-res export (see below) |
| `EXPORT_DIR` | No | `/exports` | Container path to write exports |
| `TZ` | No | `UTC` | Timezone (e.g. `America/New_York`) |
| `DRY_RUN` | No | `false` | Log all writes without executing |
| `LOG_LEVEL` | No | `INFO` | Logging level |

## Scoring prompt

`prompt.md` and `prompt_group.md` control how Gemini evaluates photos. Edit them to match your taste before deploying. The defaults are tuned for family photos for home wall display on lustre paper.

To iterate on the prompt before deploying, use `explore/03_gemini_test.py`.

## Database setup

Curio needs read-only access to the Immich Postgres database. Create a dedicated user:

```sql
CREATE USER immich_reader WITH PASSWORD 'your_password';
GRANT CONNECT ON DATABASE immich TO immich_reader;
GRANT USAGE ON SCHEMA public TO immich_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO immich_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO immich_reader;
```

Run this from a Postgres client connected to your Immich database (e.g. `psql -h localhost -U postgres immich`).

## Networking

Curio needs to reach the Immich Postgres container. The `docker-compose.yml` assumes they share a Docker network (`immich-network` by default — change this to match whatever your Immich stack names its network).

**Option A (recommended):** Join Immich's internal network:
```yaml
networks:
  immich-network:
    external: true
```

**Option B:** Expose Postgres to the host and set `DB_HOST=host.docker.internal` in `.env`. This works but means Postgres is reachable from outside Docker.

## Export to Syncthing (optional)

If you want full-resolution approved photos written to disk (e.g. for pickup by another machine):

1. Set `EXPORT_ENABLED=true` in `.env`
2. Uncomment the export volume in `docker-compose.yml` and point it at your Syncthing folder:
   ```yaml
   - /your/syncthing/path:/exports
   ```

Exported files are named `{asset_id_prefix}_{original_filename}` and tagged `print/exported` in Immich (idempotent).

## Exploration scripts

The `explore/` directory contains the Phase 0 scripts used to validate each integration before building the main pipeline. They run standalone with a `.env` file and are useful for debugging or adapting to a different Immich setup:

| Script | Purpose |
|---|---|
| `01_db_schema.py` | Introspect Immich DB schema, validate column names |
| `02_immich_api.py` | Test all Immich REST endpoints |
| `03_gemini_test.py` | Score photos with Gemini, iterate on the prompt |
| `04_telegram_test.py` | Telegram round-trip test (send photo, receive callback) |
| `05_burst_groups.py` | Discover burst group sizes in your library |
| `06_test_group_scoring.py` | Dry-run burst group scoring end-to-end |

## Limitations

- **Single user:** Scoped to one `IMMICH_USER_ID`. Multi-user support is not planned.
- **Telegram preview:** Telegram compresses photos. Full resolution is only in the export.
- **No print ordering:** The pipeline stops at "approved and exported." Ordering prints is manual.

## License

MIT — see [LICENSE](LICENSE).
