FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps:
# - ca-certificates: TLS for Telegram/GCP
# - postgresql-client: pg_dump/pg_restore for panel backups
# - tini: proper signal handling (esp. when running multiple bot workers)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tini \
    # Prefer a version-pinned client when available (avoid dump/restore quirks across major versions).
    && (apt-get install -y --no-install-recommends postgresql-client-16 || apt-get install -y --no-install-recommends postgresql-client) \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy project (secrets excluded via .dockerignore)
COPY . /app

# Runtime dirs
RUN mkdir -p /app/backups /tmp/aishield-metrics

# Run as non-root
RUN useradd -u 10001 -m appuser \
    && chown -R appuser:appuser /app /tmp/aishield-metrics
USER appuser

ENTRYPOINT ["tini", "--", "python", "docker/entrypoint.py"]
CMD ["bot"]
