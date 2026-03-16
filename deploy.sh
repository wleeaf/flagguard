#!/usr/bin/env bash
set -euo pipefail

# AIShield deployment script
# Usage: ./deploy.sh [user@host] [remote_path]
#
# Defaults can be overridden via environment variables:
#   DEPLOY_HOST    - SSH target (e.g. user@1.2.3.4)
#   DEPLOY_PATH    - Remote project directory (e.g. /opt/aishield)
#   COMPOSE_FILE   - Compose file to use (default: docker-compose.prod.yml)

DEPLOY_HOST="${1:-${DEPLOY_HOST:-}}"
DEPLOY_PATH="${2:-${DEPLOY_PATH:-/opt/aishield}}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"

if [ -z "$DEPLOY_HOST" ]; then
    echo "Usage: ./deploy.sh user@host [remote_path]"
    echo "  or set DEPLOY_HOST and DEPLOY_PATH env vars"
    exit 1
fi

echo "==> Deploying to $DEPLOY_HOST:$DEPLOY_PATH"
echo "    Compose file: $COMPOSE_FILE"

# Sync files, excluding sensitive/generated content
rsync -avz --delete \
    --exclude '.env' \
    --exclude 'api.json' \
    --exclude 'backups/' \
    --exclude '.git/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '.venv/' \
    --exclude 'venv/' \
    --exclude '.mypy_cache/' \
    --exclude '.pytest_cache/' \
    --exclude 'node_modules/' \
    ./ "$DEPLOY_HOST:$DEPLOY_PATH/"

echo "==> Files synced. Building and restarting containers..."

# Build and restart via docker compose (using production compose file)
ssh "$DEPLOY_HOST" "cd $DEPLOY_PATH && docker compose -f $COMPOSE_FILE build && docker compose -f $COMPOSE_FILE up -d"

echo "==> Waiting for containers to become healthy..."

# Health check via Docker health status (port 8000 is not exposed to host in prod)
MAX_WAIT=60
INTERVAL=5
ELAPSED=0
while [ $ELAPSED -lt $MAX_WAIT ]; do
    sleep $INTERVAL
    ELAPSED=$((ELAPSED + INTERVAL))
    HEALTH=$(ssh "$DEPLOY_HOST" "cd $DEPLOY_PATH && docker compose -f $COMPOSE_FILE ps --format '{{.Name}} {{.Status}}' 2>/dev/null" || true)
    echo "    [$ELAPSED/${MAX_WAIT}s] $HEALTH"
    if echo "$HEALTH" | grep -q "(unhealthy)"; then
        echo "==> WARNING: Unhealthy container detected. Check logs with:"
        echo "    ssh $DEPLOY_HOST 'cd $DEPLOY_PATH && docker compose -f $COMPOSE_FILE logs --tail=50'"
        exit 1
    fi
    # All services must report healthy (no "starting" or "unhealthy")
    if echo "$HEALTH" | grep -q "(healthy)" && ! echo "$HEALTH" | grep -q "(health: starting)"; then
        echo "==> Health check PASSED"
        break
    fi
done

if [ $ELAPSED -ge $MAX_WAIT ]; then
    echo "==> WARNING: Timed out waiting for healthy status. Check logs with:"
    echo "    ssh $DEPLOY_HOST 'cd $DEPLOY_PATH && docker compose -f $COMPOSE_FILE logs --tail=50'"
    exit 1
fi

echo "==> Deployment complete."
