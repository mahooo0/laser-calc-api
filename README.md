# DXF Laser Calculator

HTTP service that gives a quick price estimate for laser cutting from a DXF file.
It computes cut length, pierce count (closed contours plus stitched open paths),
part area, and the total price from a configurable price catalog. Each request
is logged to `orders.csv` and (optionally) pushed to Telegram.

## Layout

| Path | Purpose |
|------|---------|
| `src/laser_calc/` | Calculation core: DXF parsing via `ezdxf`, geometry |
| `src/laser_calc_api/` | Flask HTTP layer: `app.py`, `config.py`, `services.py`, `notifications.py`, `logging_config.py`, `wsgi.py` |
| `prices_catalog.json` | Price book: grades, thicknesses, per-meter / per-pierce / per-m² prices |
| `orders.csv` | Append-only order log (column layout documented below) |
| `web/index.html` | Standalone frontend used for manual smoke tests |
| `tests/` | Pytest suite (58 tests) |
| `gunicorn.conf.py` | Gunicorn config used for the production-like run (JSON logs) |
| `Dockerfile` / `docker-compose.yml` | Container packaging |
| `.env.example` | Environment variable template |

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Returns `{"ok": true}` — healthcheck target |
| `GET` | `/api/catalog` | Lists grades and thicknesses from `prices_catalog.json` |
| `POST` | `/api/calculate` | `multipart/form-data` with the DXF and client contact fields |
| `GET` | `/` | Static HTML frontend used for manual testing |

`POST /api/calculate` form fields:

- `file` — DXF (≤ 10 MB, configurable via `MAX_UPLOAD_BYTES`)
- `metal_grade`, `metal_thickness`, `quantity`
- `client_name` or `company_name`, `phone`, `email`
- (optional) `tol`, `layers`

## Local development

Requires Python 3.12+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

```bash
pytest                                   # 58 tests
ruff check . && ruff format --check .    # lint + format
flask --app laser_calc_api.app run --port 8080
# open http://127.0.0.1:8080
```

## Running in Docker (locally)

Before starting, make sure `prices_catalog.json` and `orders.csv` exist on the
host — they are bind-mounted into the container, and Compose will create empty
directories in their place if the files are missing.

```bash
cp .env.example .env       # then edit ALLOWED_ORIGINS, limits, Telegram, etc.
docker compose up -d
docker compose logs -f api
curl http://localhost:8080/api/health
```

Stop:

```bash
docker compose down
```

Prices are edited directly on the host — `prices_catalog.json` is mounted
read-only into the container, and the next request reads the updated file
without a restart.

Orders are written to `./orders.csv` on the host through the same volume.

## Configuration

Everything is set via environment variables. See `.env.example`:

| Var | Default | Description |
|-----|---------|-------------|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8080` | Bind port |
| `ALLOWED_ORIGINS` | `*` | Comma-separated CORS allow-list |
| `MAX_UPLOAD_BYTES` | `10485760` | Upload limit (10 MB) |
| `PRICES_CATALOG_PATH` | `./prices_catalog.json` | Path to the price catalog |
| `ORDERS_CSV_PATH` | `./orders.csv` | Path to the order log |
| `WEB_DIR` | `./web` | Static frontend directory |
| `LOG_LEVEL` | `INFO` | Log level |
| `GUNICORN_WORKERS` | `2` | Gunicorn worker count |
| `TELEGRAM_BOT_TOKEN` | — | Bot token from @BotFather; empty disables Telegram |
| `TELEGRAM_CHAT_IDS` | — | Comma-separated chat IDs (negative for groups) |
| `TELEGRAM_ENABLED` | `true` | Kill switch (overrides token presence) |
| `TELEGRAM_TIMEOUT_SECONDS` | `5` | Per-request timeout for the Telegram API |

## `prices_catalog.json` format

```json
{
  "grades": {
    "Ст3": {
      "1.0": { "price_meter": 20.0, "price_pierce": 1.8, "material_m2": 740.0 },
      "2.0": { "price_meter": 24.0, "price_pierce": 2.2, "material_m2": 920.0 }
    },
    "New Grade": {
      "5.0": { "price_meter": 40.0, "price_pierce": 3.5, "material_m2": 1800.0 }
    }
  }
}
```

To add a grade or thickness, edit the file directly — no restart required.
Invalid JSON does not crash the service: `/api/catalog` returns 400 with the
underlying error, all other endpoints keep working.

## `orders.csv` format

Columns: `created_at_utc, file_name, client_name, company_name, phone, email,
metal_grade, metal_thickness_mm, quantity, price_meter, price_pierce, material_m2,
cut_length_m_per_part, pierces_per_part, area_m2_per_part, price_total_per_part,
price_total_batch, status`.

`status` defaults to `new`; the manager updates it manually or via a script.

## Telegram notifications

Each successful calculation is sent as an HTML message to the configured chats.
If the Telegram API is unreachable or the token is wrong, the order still lands
in `orders.csv`, the client receives 200, and the failure is logged as
`telegram_send_failed`.

**Getting a bot token and a chat ID:**

1. In Telegram: open [@BotFather](https://t.me/BotFather) → `/newbot` → name → username (must end with `_bot`).
   You receive a token like `7891234567:AAFsdf...`
2. Find a `chat_id`:
   - **Direct chat:** send any message to your bot, then open
     `https://api.telegram.org/bot<TOKEN>/getUpdates` — `chat.id` is a positive integer.
   - **Group:** add the bot to the group, send any message in the group,
     open the same URL. `chat.id` will be a negative integer.
3. Drop them into `.env`:
   ```
   TELEGRAM_BOT_TOKEN=7891234567:AAFsdf...
   TELEGRAM_CHAT_IDS=123456789,-1001234567890
   ```
4. `docker compose up -d --force-recreate`

## Deployment

Deployment instructions for a VPS will be added once the target environment is
chosen. The intended path: `docker compose up -d` on a fresh Ubuntu 22.04+ host
with Docker Engine 24+. Before going live:

1. Replace `ALLOWED_ORIGINS=*` with the real client domain(s).
2. Put nginx or Caddy in front of the container for HTTPS (Let's Encrypt).
3. Decide on a rotation strategy for `orders.csv`, or migrate it to Google
   Sheets in Phase 2.
