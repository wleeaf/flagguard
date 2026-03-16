#!/usr/bin/env bash
# Certbot deploy hook — reloads Caddy after Let's Encrypt cert renewal.
#
# Install:
#   sudo cp /opt/aishield/deploy/reload-caddy.sh \
#           /etc/letsencrypt/renewal-hooks/deploy/reload-caddy.sh
#   sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-caddy.sh
#
# Certbot runs all scripts in renewal-hooks/deploy/ after a successful renewal.

set -euo pipefail

COMPOSE_FILE="/opt/aishield/docker-compose.prod.yml"

if [ -f "$COMPOSE_FILE" ]; then
    docker compose -f "$COMPOSE_FILE" exec -T caddy caddy reload --config /etc/caddy/Caddyfile 2>&1 \
        | logger -t certbot-caddy-reload
fi
