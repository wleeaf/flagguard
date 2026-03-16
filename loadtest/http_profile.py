#!/usr/bin/env python3
"""HTTP profile for panel/metrics endpoints under concurrent load."""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import httpx


async def _worker(
    name: str,
    client: httpx.AsyncClient,
    path: str,
    stop_at: float,
    latencies: list[float],
    failures: list[int],
):
    while time.monotonic() < stop_at:
        started = time.monotonic()
        try:
            resp = await client.get(path)
            if resp.status_code >= 400:
                failures[0] += 1
        except Exception:
            failures[0] += 1
        latencies.append(time.monotonic() - started)


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int((len(ordered) - 1) * p)
    return ordered[idx]


async def main():
    parser = argparse.ArgumentParser(description="Run HTTP load profile for panel endpoints")
    parser.add_argument("--base-url", required=True, help="Example: http://127.0.0.1:8000")
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--endpoint", default="/health", help="Endpoint path to stress")
    parser.add_argument(
        "--max-failure-rate",
        type=float,
        default=0.01,
        help="Gate: max allowed failure ratio (0.01 = 1%%). Use negative to disable.",
    )
    parser.add_argument(
        "--max-p95-ms",
        type=float,
        default=1000,
        help="Gate: max allowed p95 latency in milliseconds. Use negative to disable.",
    )
    parser.add_argument(
        "--max-p99-ms",
        type=float,
        default=2500,
        help="Gate: max allowed p99 latency in milliseconds. Use negative to disable.",
    )
    parser.add_argument(
        "--min-rps",
        type=float,
        default=100.0,
        help="Gate: minimum allowed throughput RPS. Use negative to disable.",
    )
    args = parser.parse_args()

    latencies: list[float] = []
    failures = [0]
    stop_at = time.monotonic() + args.duration

    timeout = httpx.Timeout(10.0, connect=5.0)
    limits = httpx.Limits(max_connections=args.concurrency * 2, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=timeout, limits=limits) as client:
        tasks = [
            asyncio.create_task(
                _worker(f"w{i}", client, args.endpoint, stop_at, latencies, failures)
            )
            for i in range(args.concurrency)
        ]
        await asyncio.gather(*tasks)

    total = len(latencies)
    rps = total / args.duration if args.duration > 0 else 0.0
    avg = statistics.fmean(latencies) if latencies else 0.0
    p50 = _percentile(latencies, 0.50)
    p95 = _percentile(latencies, 0.95)
    p99 = _percentile(latencies, 0.99)
    failure_rate = (failures[0] / total) if total else 0.0

    print("=== HTTP Profile Report ===")
    print(f"base_url={args.base_url}")
    print(f"endpoint={args.endpoint}")
    print(f"concurrency={args.concurrency}")
    print(f"duration_s={args.duration}")
    print(f"requests={total}")
    print(f"failures={failures[0]}")
    print(f"failure_rate={failure_rate:.4f}")
    print(f"throughput_rps={rps:.2f}")
    print(f"latency_avg_ms={avg * 1000:.1f}")
    print(f"latency_p50_ms={p50 * 1000:.1f}")
    print(f"latency_p95_ms={p95 * 1000:.1f}")
    print(f"latency_p99_ms={p99 * 1000:.1f}")

    violations: list[str] = []
    if args.max_failure_rate >= 0 and failure_rate > args.max_failure_rate:
        violations.append(
            f"failure_rate {failure_rate:.4f} > max_failure_rate {args.max_failure_rate:.4f}"
        )
    if args.max_p95_ms >= 0 and (p95 * 1000.0) > args.max_p95_ms:
        violations.append(
            f"p95_ms {p95 * 1000.0:.1f} > max_p95_ms {args.max_p95_ms:.1f}"
        )
    if args.max_p99_ms >= 0 and (p99 * 1000.0) > args.max_p99_ms:
        violations.append(
            f"p99_ms {p99 * 1000.0:.1f} > max_p99_ms {args.max_p99_ms:.1f}"
        )
    if args.min_rps >= 0 and rps < args.min_rps:
        violations.append(f"throughput_rps {rps:.2f} < min_rps {args.min_rps:.2f}")

    if violations:
        print("GATE: FAIL")
        for v in violations:
            print(f"- {v}")
        raise SystemExit(1)

    print("GATE: PASS")


if __name__ == "__main__":
    asyncio.run(main())
