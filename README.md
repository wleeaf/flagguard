# AIShield (ai-telegram-bot)

AIShield is a Telegram bot that routes user messages to **Gemini (Vertex AI)** and includes a **FastAPI admin panel** for monitoring, backups, and operational controls.

This README focuses on the simplest path to clone, configure, and deploy on Ubuntu 24.04 with PostgreSQL.

## Docker (Easy Mode, Recommended)

If you use Docker, you do not need to install PostgreSQL on the host. Docker Compose will run:
- Postgres
- the bot
- the admin panel

### Install Docker (Ubuntu 24.04)

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
newgrp docker
```

### Local (Polling + Panel)

1. Create `.env`:

```bash
cp .env.example .env
```

Minimum keys you must fill:
- `TELEGRAM_TOKEN`
- `ADMIN_IDS`
- `CHALLENGE_FLAG`
- `PANEL_SECRET_KEY`
- `SERVICE_ACCOUNT_PATH` (defaults to `api.json`)

2. Put your Vertex service account JSON at `./api.json` (same directory as `Dockerfile`).

3. Start everything:

```bash
docker compose up -d --build
```

Panel:
- `http://127.0.0.1:8000`

Create the first panel admin:

```bash
docker compose exec panel python -m panel.cli create-admin
```

### Production (Webhook + HTTPS via Caddy)

Requirements:
- a domain pointing to your server (A/AAAA record)
- ports `80` and `443` open

1. In `.env`, set at least:
- `APP_ENV=production`
- `WEBHOOK_URL=https://yourdomain.com` (your domain)
- `WEBHOOK_SECRET_TOKEN` (>= 16 chars)
- `DOMAIN=yourdomain.com` (used by Caddy)
- `POSTGRES_PASSWORD` (set a strong password)

2. Start production stack:

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

Notes:
- The webhook endpoint will be `https://yourdomain.com/webhook` (controlled by `WEBHOOK_PATH`).
- `BOT_WEBHOOK_WORKERS` controls how many bot processes run inside the container. On 4 vCPU, `8` is a reasonable starting point.

## 1) Clone

```bash
git clone <REPO_URL> ai-telegram-bot
cd ai-telegram-bot
```

## 2) Install System Packages (Ubuntu 24.04)

```bash
sudo apt update
sudo apt install -y git python3-venv python3-pip postgresql postgresql-contrib postgresql-client
sudo systemctl enable --now postgresql
```

Notes:
- `postgresql-client` provides `pg_dump` and `pg_restore` (needed for panel backups/restores).

## 3) Create a Python Virtualenv + Install Dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 4) PostgreSQL Setup

### 4.1 Create DB user + database

```bash
sudo -u postgres psql
```

In the `psql` prompt:

```sql
CREATE USER aishield WITH PASSWORD 'CHANGE_ME';
CREATE DATABASE aishield OWNER aishield;
GRANT ALL PRIVILEGES ON DATABASE aishield TO aishield;
\q
```

### 4.2 Verify connection

```bash
psql "postgresql://aishield:CHANGE_ME@127.0.0.1:5432/aishield" -c "select now();"
```

## 5) Vertex AI (Gemini) Setup

This project uses the **Vertex AI** backend via a **service account JSON key**.

You need:
- A GCP project with Vertex AI enabled
- A service account with permissions to call Vertex AI
- A downloaded JSON key file

Place the JSON key on disk (example: project root):

```bash
cp /path/to/service-account.json ./api.json
chmod 600 ./api.json
```

## 6) Configure `.env`

Copy the example:

```bash
cp .env.example .env
```

Minimum required keys (the bot will refuse to start if these are missing):

```env
# Telegram
TELEGRAM_TOKEN=123456:ABCDEF...
ADMIN_IDS=6596077288  # comma-separated list is supported

# PostgreSQL
DATABASE_URL=postgresql://aishield:CHANGE_ME@127.0.0.1:5432/aishield

# Vertex AI (Gemini)
SERVICE_ACCOUNT_PATH=api.json
GCP_LOCATION=us-central1
GEMINI_MODEL=gemini-2.0-flash-001

# Challenge
CHALLENGE_FLAG=FLAG{your_flag_here}

# Panel auth (required even if you only run the bot)
PANEL_SECRET_KEY=REPLACE_WITH_A_LONG_RANDOM_SECRET

# Local dev defaults
APP_ENV=development
BOT_MODE=polling
PANEL_COOKIE_SECURE=false
```

Generate a good `PANEL_SECRET_KEY`:

```bash
openssl rand -hex 32
```

For a full list of optional knobs, read `.env.example` and `config.py`.

## 7) Run Locally (Polling Mode)

Polling is the simplest for local development:

```bash
source venv/bin/activate
python bot.py
```

The bot auto-creates tables on startup (PostgreSQL schema is managed in `database.py`).

## 8) Run the Admin Panel (Optional)

Create a panel admin user:

```bash
source venv/bin/activate
python -m panel.cli create-admin
```

Run the panel:

```bash
python -m panel.cli run
```

Default panel URL:
- `http://127.0.0.1:8000`

If you serve the panel over HTTPS in production, keep `PANEL_COOKIE_SECURE=true`.

## 9) Production Deployment (Recommended)

### 9.1 Install to `/opt/aishield`

```bash
sudo useradd -r -m -d /opt/aishield -s /usr/sbin/nologin aishield || true
sudo mkdir -p /opt/aishield
sudo chown -R aishield:aishield /opt/aishield
```

Clone the repo on the server (as the service user):

```bash
sudo -u aishield git clone <REPO_URL> /opt/aishield
```

Create venv + install deps:

```bash
sudo -u aishield bash -lc "cd /opt/aishield && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
```

Put your `.env` in `/opt/aishield/.env` and your service account JSON at the path referenced by `SERVICE_ACCOUNT_PATH`:

```bash
sudo -u aishield cp /opt/aishield/.env.example /opt/aishield/.env
sudo -u aishield nano /opt/aishield/.env
sudo -u aishield chmod 600 /opt/aishield/.env
sudo -u aishield chmod 600 /opt/aishield/api.json
```

If you will run the panel, create the first panel admin once:

```bash
sudo -u aishield bash -lc "cd /opt/aishield && source venv/bin/activate && python -m panel.cli create-admin"
```

### 9.2 Use systemd services

This repo includes ready-to-copy unit files in `deploy/`:
- `deploy/aishield-bot.service` (polling)
- `deploy/aishield-bot-webhook@.service` (webhook workers)
- `deploy/aishield-panel.service`

Install them:

```bash
sudo cp deploy/aishield-bot.service /etc/systemd/system/
sudo cp deploy/aishield-bot-webhook@.service /etc/systemd/system/
sudo cp deploy/aishield-panel.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Start polling bot:

```bash
sudo systemctl enable --now aishield-bot.service
```

Start panel:

```bash
sudo systemctl enable --now aishield-panel.service
```

Logs:

```bash
journalctl -u aishield-bot.service -f
journalctl -u aishield-panel.service -f
```

## 10) Webhook Mode (For Real Production Traffic)

Polling is fine for dev, but webhook mode is the right choice for higher traffic and lower latency.

Set in `.env`:

```env
APP_ENV=production
BOT_MODE=webhook
WEBHOOK_URL=https://yourdomain.com
WEBHOOK_PATH=/webhook
WEBHOOK_HOST=0.0.0.0
WEBHOOK_PORT=8443
WEBHOOK_SECRET_TOKEN=PUT_A_RANDOM_16+_CHAR_SECRET_HERE
WEBHOOK_MAX_CONNECTIONS=40
WEBHOOK_REUSE_PORT=true
```

Important:
- Telegram requires a **public HTTPS** URL in production.
- You typically run a reverse proxy (Caddy/Nginx) for TLS and forward to `127.0.0.1:8443`.

Start 1..N webhook workers (example: 8):

```bash
sudo systemctl enable --now aishield-bot-webhook@{1..8}
```

Tip for multi-worker webhook setups:
- Set `WEBHOOK_REGISTER_ON_START=false` in `.env` so workers do not constantly call Telegram `setWebhook`.
- Run one worker with registration enabled when you need to (for example, start it manually once with `WEBHOOK_REGISTER_ON_START=true`).

Health and metrics endpoints (webhook server):
- `GET /health`
- `GET /metrics`

Protect them behind firewall or reverse proxy ACLs in production.

## 11) Backups / Restore / Reset

The panel Controls page can:
- create backups using `pg_dump` (`.dump`)
- restore from backups using `pg_restore`
- reset the database (keep admins or wipe all)

Requirements:
- `pg_dump` and `pg_restore` installed on the host (`postgresql-client`)
- enable Maintenance Mode before destructive actions

Backups are stored under `PANEL_BACKUP_DIR` (default: `./backups`).

## 12) Customization

You can rebrand the bot and flag format without touching source code by setting two environment variables in `.env`:

| Variable | Default | Description |
|---|---|---|
| `BOT_NAME` | `AIShield` | Display name used in bot responses, panel title, and system messages. |
| `FLAG_PREFIX` | `FLAG` | Prefix for the challenge flag format, e.g. `FLAG{...}`. Change this to use your own CTF branding. |

Example:

```env
BOT_NAME=MyCTFBot
FLAG_PREFIX=CTF
CHALLENGE_FLAG=CTF{my_secret_flag}
```

These values can also be changed at runtime through the admin panel Controls page (Bot Name and Flag Prefix fields). Panel overrides take priority over environment variables until the panel values are cleared.

## 13) Common Notes

- Some Telegram users do not have a username. In that case the panel shows `-` (expected).
- `.env.example` is the source of truth for all optional tuning knobs.
- If the bot prints “AI Model initialization failed”, your Vertex credentials/model/location are wrong or the service account lacks permissions.
