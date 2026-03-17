# FlagGuard

An AI-powered Telegram bot for CTF competitions. Players try to extract a secret flag from an AI that actively defends it. Comes with a web admin panel, multi-flag sequential challenges, difficulty tiers, and a full security stack.

Built on Google Gemini (Vertex AI), PostgreSQL, and Docker.

---

## Table of Contents

- [How It Works](#how-it-works)
- [Features](#features)
- [Quick Start (Docker)](#quick-start-docker)
- [Production Deployment](#production-deployment)
- [Configuration Reference](#configuration-reference)
- [Manual Setup (No Docker)](#manual-setup-no-docker)
- [Architecture](#architecture)

---

## How It Works

1. You define one or more flags in the admin panel.
2. The AI bot receives the flags in its system prompt and is instructed to protect them.
3. Players message the bot on Telegram, trying to trick the AI into revealing the flag.
4. The bot uses layered defenses (jailbreak detection, output sanitization, honeypot traps, behavior analysis) to prevent leaks.
5. When a player submits the correct flag, the bot records their solve and optionally notifies admins.

The difficulty level controls how hard the AI defends. At EASY, the bot is generous with hints. At IMPOSSIBLE, it blocks every known extraction technique.

---

## Features

### Multi-Flag CTF System

- Sequential flag progression: players must solve flags in order.
- Per-flag configurable success messages, tags, and admin notifications.
- Full progress tracking and leaderboard.
- Manage everything from the web panel: create, edit, delete, reorder flags.

### Four Difficulty Tiers

| Level | Behavior |
|---|---|
| Easy | Generous hints, creative flag delivery allowed, minimal guardrails. |
| Medium | No full flag, but thematic hints reward creative approaches. |
| Hard | Strict defense, only structural hints for genuinely creative attempts. |
| Impossible | Zero tolerance. Flag never delivered. All extraction techniques blocked. |

Each tier has its own AI temperature, context window size, message length limits, jailbreak thresholds, and honeypot activation rates. Difficulty can be changed at runtime from the panel or Telegram.

### Security Stack

**Jailbreak Detection** -- 50+ weighted patterns covering direct bypass attempts, encoding requests, role-play framing, Turkish-language variants, repetition abuse, entropy anomalies, delimiter flooding, and special character density. Score-based: each pattern contributes points, and the threshold varies by difficulty.

**Output Sanitization** -- Blocks the literal flag string, flag format patterns, partial flag substring leaks, encoded content (base64), and system prompt metadata keywords from appearing in AI responses.

**Honeypot System** -- Serves fake admin panels, error messages, debug output, and database dumps to trap social engineering attempts. Probability-based activation that scales with difficulty.

**Behavior Analysis** -- Tracks per-user jailbreak ratios, suspicion scores, and honeypot catches. Flags high-risk users automatically.

### AI Configuration

- Model: Google Gemini via Vertex AI (configurable model ID).
- Per-difficulty system prompts with shared personality rules and independent security documents.
- Editable from the web panel: global personality and per-difficulty security docs with live preview.
- Supports image, audio, and video attachments in messages.
- Conversation history per user (configurable depth per difficulty).

### Web Admin Panel

- **Dashboard** -- Live statistics, request charts (24h/3d/7d/30d), recent activity, competition state.
- **Prompt Editor** -- Edit shared personality and per-difficulty security documents with placeholder preview.
- **Flag Management** -- Full CRUD for CTF flags with drag-and-drop reordering.
- **User Management** -- Behavior stats, conversation history, per-user actions.
- **Moderation** -- Ban, timeout, unban users with configurable durations.
- **Competition Manager** -- Start timed rounds, track winners, manage leaderboards.
- **Logs** -- Conversation logs, message logs, admin messages, bot events, audit trail.
- **Reports** -- View and resolve user-submitted reports.
- **Controls** -- Toggle maintenance/silent modes, change difficulty, manage backups, database reset.
- **Backup/Restore** -- PostgreSQL dumps via pg_dump, restore from panel, configurable retention.

### Competition System

Separate from the flag challenges. Admins post a question and an answer. Players race to answer correctly. Supports max winner limits, timed deadlines, auto-end, and persistent leaderboards.

### Telegram Commands

**Players:** `/start`, `/help`, `/difficulty`, `/reset`, `/report`, `/answer`, `/id`

**Admins:** `/stats`, `/user`, `/ban`, `/unban`, `/timeout`, `/untimeout`, `/dm`, `/broadcast`, `/backup`, `/maintenance`, `/silent`, `/competition_setup`, `/competition_end`, `/competition_status`, `/leaderboard`, `/set_maxwinner_count`, `/top_jailbreakers`

### Operational

- Circuit breaker with automatic recovery for AI API failures.
- Global RPM quota shared across workers via PostgreSQL.
- Three-layer rate limiting (per-user cooldown, sliding window, global RPM).
- Async batched message logging to reduce database pressure.
- Durable broadcast queue with retry logic.
- Prometheus-compatible metrics endpoint.
- Multi-worker webhook mode with SO_REUSEPORT for horizontal scaling.
- Graceful shutdown with queue draining.
- Auto-cleanup of old logs (configurable retention).

---

## Quick Start (Docker)

### Prerequisites

- Docker and Docker Compose
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- A Google Cloud service account JSON key with Vertex AI access

### 1. Clone and configure

```bash
git clone https://github.com/ts-solidarity/flagguard.git
cd flagguard
cp .env.example .env
```

Edit `.env` and fill in the required values:

```env
TELEGRAM_TOKEN=your_bot_token
ADMIN_IDS=your_telegram_user_id
CHALLENGE_FLAG=FLAG{your_secret_flag}
PANEL_SECRET_KEY=run_openssl_rand_hex_32
DATABASE_URL=postgresql://aishield:change-me@postgres:5432/aishield
```

### 2. Add your service account key

```bash
cp /path/to/your-service-account.json ./api.json
```

### 3. Start

```bash
docker compose up -d --build
```

This starts PostgreSQL, the bot (polling mode), and the admin panel.

### 4. Create the first admin user

```bash
docker compose exec panel python -m panel.cli create-admin
```

Panel is at `http://localhost:8000`.

The bot auto-creates all database tables on first startup. The `CHALLENGE_FLAG` from your `.env` is automatically seeded as the first CTF flag.

---

## Production Deployment

Production mode uses webhook delivery, multiple bot workers, HTTPS via Caddy, and hardened settings.

### Prerequisites

- A server with a domain name pointing to it (A/AAAA record).
- Ports 80 and 443 open.

### 1. Configure `.env` for production

In addition to the required values above, set:

```env
APP_ENV=production
BOT_MODE=webhook
WEBHOOK_URL=https://yourdomain.com
WEBHOOK_SECRET_TOKEN=a_random_string_at_least_16_chars
DOMAIN=yourdomain.com
POSTGRES_PASSWORD=a_strong_password
PANEL_COOKIE_SECURE=true
```

### 2. Start the production stack

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

This runs PostgreSQL, the bot (webhook mode with 4 workers by default), the admin panel, and Caddy for automatic HTTPS.

### 3. Create a panel admin

```bash
docker compose -f docker-compose.prod.yml exec panel python -m panel.cli create-admin
```

### Scaling

`BOT_WEBHOOK_WORKERS` controls the number of bot processes inside the container. On a 4-vCPU machine, 4-8 workers is a reasonable starting point. Workers share the webhook port via SO_REUSEPORT.

### Deploy script

A `deploy.sh` script is included for rsync-based deployments:

```bash
./deploy.sh user@yourserver /opt/flagguard
```

It syncs files (excluding secrets), rebuilds containers, and waits for health checks to pass.

### Health endpoints

- `GET /health` -- returns 200 if the database is reachable.
- `GET /metrics` -- Prometheus-compatible metrics.

Both are served by the webhook server and the panel. Restrict access in production.

---

## Configuration Reference

All configuration is done through environment variables in `.env`. See `.env.example` for the full list with descriptions.

### Required

| Variable | Description |
|---|---|
| `TELEGRAM_TOKEN` | Bot token from BotFather. |
| `ADMIN_IDS` | Comma-separated Telegram user IDs for admin access. |
| `CHALLENGE_FLAG` | The flag string (e.g. `FLAG{secret}`). Auto-seeded as CTF flag #1. |
| `SERVICE_ACCOUNT_PATH` | Path to GCP service account JSON. Default: `api.json`. |
| `DATABASE_URL` | PostgreSQL connection string. |
| `PANEL_SECRET_KEY` | Secret for session signing. Generate with `openssl rand -hex 32`. |

### Branding

| Variable | Default | Description |
|---|---|---|
| `BOT_NAME` | `AIShield` | Display name in bot responses and the panel. |
| `FLAG_PREFIX` | `FLAG` | Prefix for flag format (e.g. `FLAG{...}`). |

Both can be overridden at runtime from the panel Controls page.

### AI Tuning

| Variable | Default | Description |
|---|---|---|
| `GEMINI_MODEL` | `gemini-3.0-flash` | Vertex AI model ID. |
| `GCP_LOCATION` | `us-central1` | Vertex AI region. |
| `AI_API_TIMEOUT_SECONDS` | `15` | Per-request timeout. |
| `AI_RETRY_ATTEMPTS` | `2` | Retries on transient errors. |
| `AI_CIRCUIT_BREAKER_THRESHOLD` | `10` | Consecutive failures before circuit opens. |
| `AI_GLOBAL_RPM_LIMIT` | `800` | Shared request budget across all workers. 0 disables. |
| `AI_MAX_CONCURRENT_REQUESTS` | `50`/`8` | Per-worker concurrency (dev/prod defaults differ). |

### Webhook

| Variable | Default | Description |
|---|---|---|
| `BOT_MODE` | `polling` | Set to `webhook` for production. |
| `WEBHOOK_URL` | -- | Public HTTPS URL (required in webhook mode). |
| `WEBHOOK_SECRET_TOKEN` | -- | Required in production, at least 16 characters. |
| `WEBHOOK_PORT` | `8443` | Internal port the webhook server listens on. |
| `BOT_WEBHOOK_WORKERS` | `4` | Number of bot processes per container. |

### Panel

| Variable | Default | Description |
|---|---|---|
| `PANEL_PORT` | `8000` | Panel HTTP port. |
| `PANEL_COOKIE_SECURE` | `true` | Set to `false` for local development without HTTPS. |
| `PANEL_BACKUP_RETENTION` | `20` | Number of backup files to keep. |

---

## Manual Setup (No Docker)

### System packages (Ubuntu 24.04)

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip postgresql postgresql-contrib postgresql-client
sudo systemctl enable --now postgresql
```

### Database

```bash
sudo -u postgres psql -c "CREATE USER flagguard WITH PASSWORD 'CHANGE_ME';"
sudo -u postgres psql -c "CREATE DATABASE flagguard OWNER flagguard;"
```

### Application

```bash
git clone https://github.com/ts-solidarity/flagguard.git
cd flagguard
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your configuration
```

### Run

```bash
# Bot (polling mode)
python bot.py

# Panel (separate terminal)
python -m panel.cli create-admin
python -m panel.cli run
```

### Systemd (optional)

Unit files are provided in `deploy/` for running the bot and panel as system services.

---

## Architecture

```
Telegram
  |
  v
[Caddy] --> /webhook --> [Bot Workers x N]
         --> /*       --> [Panel]
                              |
                     [PostgreSQL 16]
```

**Bot** -- Python 3.12, aiogram (Telegram framework), google-genai (Vertex AI client). Runs in polling or webhook mode. Multi-worker support via SO_REUSEPORT.

**Panel** -- FastAPI + Jinja2 templates. Serves the admin interface and health/metrics endpoints.

**Database** -- PostgreSQL 16. All tables are auto-created on first startup. Schema is managed in `database.py`.

**Caddy** -- Reverse proxy with automatic HTTPS (Let's Encrypt). Routes `/webhook` to the bot and everything else to the panel. Only used in production Docker deployment.

### Key directories

```
ai/            AI engine, difficulty profiles, personality definitions
handlers/      Telegram command and message handlers
security/      Jailbreak detection, honeypot, sanitizer, behavior analysis
models/        Database repositories (user, conversation, competition, flags, etc.)
panel/         Web admin panel (routes, templates, CLI)
docker/        Entrypoint script and Caddyfile
deploy/        Systemd unit files
```

---

## License

MIT
