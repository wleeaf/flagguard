#!/usr/bin/env python3
"""Synthetic long-horizon concurrency profile for AIShield internals.

This script stress-tests the in-process bot pipeline without calling real Gemini.
It mocks AI latency and drives concurrent `engine.get_response(...)` calls.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time

# Allow running as `python loadtest/synthetic_concurrency.py` from project root.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from database import init_database
from notifications import start_broadcast_worker, stop_broadcast_worker
from services import engine


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text


class _FakeModels:
    def __init__(self, latency_s: float):
        self.latency_s = latency_s

    async def generate_content(self, **_kwargs):
        await asyncio.sleep(self.latency_s)
        return _FakeResponse("Mocked AI response")


class _FakeAio:
    def __init__(self, latency_s: float):
        self.models = _FakeModels(latency_s)

    async def aclose(self):
        return None


class _FakeClient:
    def __init__(self, latency_s: float):
        self.aio = _FakeAio(latency_s)

    def close(self):
        return None


async def _user_loop(
    user_idx: int,
    stop_at: float,
    latencies: list[float],
    failures: list[int],
):
    uid = f"load_{user_idx}"
    first = f"user{user_idx}"
    while time.monotonic() < stop_at:
        started = time.monotonic()
        try:
            await engine.get_response("hello from load test", uid, first, first, [])
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
    parser = argparse.ArgumentParser(description="Run synthetic AIShield load profile")
    parser.add_argument("--users", type=int, default=1000, help="Concurrent synthetic users")
    parser.add_argument("--duration", type=int, default=60, help="Duration in seconds")
    parser.add_argument(
        "--mock-ai-latency-ms",
        type=int,
        default=300,
        help="Mock Gemini latency in milliseconds",
    )
    parser.add_argument(
        "--max-failure-rate",
        type=float,
        default=0.01,
        help="Gate: max allowed failure ratio (0.01 = 1%%). Use negative to disable.",
    )
    parser.add_argument(
        "--max-p95-ms",
        type=float,
        default=3000,
        help="Gate: max allowed p95 latency in milliseconds. Use negative to disable.",
    )
    parser.add_argument(
        "--max-p99-ms",
        type=float,
        default=7000,
        help="Gate: max allowed p99 latency in milliseconds. Use negative to disable.",
    )
    parser.add_argument(
        "--min-rps",
        type=float,
        default=40.0,
        help="Gate: minimum allowed throughput RPS. Use negative to disable.",
    )
    args = parser.parse_args()

    init_database()
    await start_broadcast_worker("loadtest")
    try:
        engine.client = _FakeClient(args.mock_ai_latency_ms / 1000.0)
        latencies: list[float] = []
        failures = [0]
        stop_at = time.monotonic() + args.duration
        tasks = [
            asyncio.create_task(_user_loop(i, stop_at, latencies, failures))
            for i in range(args.users)
        ]
        await asyncio.gather(*tasks)
    finally:
        await stop_broadcast_worker()
        await engine.aclose()

    total = len(latencies)
    elapsed = float(args.duration)
    rps = total / elapsed if elapsed > 0 else 0.0
    p50 = _percentile(latencies, 0.50)
    p95 = _percentile(latencies, 0.95)
    p99 = _percentile(latencies, 0.99)
    avg = statistics.fmean(latencies) if latencies else 0.0
    failure_rate = (failures[0] / total) if total else 0.0

    print("=== Synthetic Concurrency Report ===")
    print(f"users={args.users}")
    print(f"duration_s={args.duration}")
    print(f"mock_ai_latency_ms={args.mock_ai_latency_ms}")
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
