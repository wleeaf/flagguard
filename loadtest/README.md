# Load Testing

## 1. Synthetic Bot Pipeline Test

Runs the internal bot pipeline with mocked AI latency and high concurrency:

```bash
python3 loadtest/synthetic_concurrency.py --users 1000 --duration 120 --mock-ai-latency-ms 300
```

Strict gate example (fails with exit code 1 if SLOs are not met):

```bash
python3 loadtest/synthetic_concurrency.py \
  --users 1000 \
  --duration 180 \
  --mock-ai-latency-ms 300 \
  --max-failure-rate 0.01 \
  --max-p95-ms 3000 \
  --max-p99-ms 7000 \
  --min-rps 40
```

What it validates:
- Request throughput under 1000 concurrent logical users.
- p50/p95/p99 end-to-end bot pipeline latency.
- Failure behavior without depending on external Gemini quota.

## 2. HTTP Endpoint Profile

Load test panel/metrics endpoints on a running deployment:

```bash
python3 loadtest/http_profile.py --base-url http://127.0.0.1:8000 --endpoint /metrics --concurrency 200 --duration 60
```

Strict gate example:

```bash
python3 loadtest/http_profile.py \
  --base-url http://127.0.0.1:8000 \
  --endpoint /metrics \
  --concurrency 200 \
  --duration 120 \
  --max-failure-rate 0.01 \
  --max-p95-ms 1000 \
  --max-p99-ms 2500 \
  --min-rps 100
```

What it validates:
- `/health` and `/metrics` responsiveness under concurrent scraping/traffic.
- Basic panel control-plane HTTP headroom.

## Notes

- For realistic production tests, run these against PostgreSQL-backed deployment settings.
- Pair with metrics scraping and alert rules in `deploy/prometheus-alerts.yml`.
