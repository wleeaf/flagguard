"""Lightweight in-process metrics registry."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import defaultdict

# Separate locks per metric type to reduce contention on the hot path.
_counter_lock = threading.Lock()
_gauge_lock = threading.Lock()
_histogram_lock = threading.Lock()

_counters: dict[str, float] = defaultdict(float)
_gauges: dict[str, float] = defaultdict(float)

_DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

_histograms: dict[str, dict[str, float]] = defaultdict(
    lambda: {"count": 0.0, "sum": 0.0, "max": 0.0}
)

_export_task: asyncio.Task | None = None
_export_stop_event: asyncio.Event | None = None
_export_loop: asyncio.AbstractEventLoop | None = None
_export_path: str | None = None


def inc_counter(name: str, value: float = 1.0) -> None:
    with _counter_lock:
        _counters[name] += value


def set_gauge(name: str, value: float) -> None:
    with _gauge_lock:
        _gauges[name] = value


def add_gauge(name: str, delta: float) -> None:
    with _gauge_lock:
        _gauges[name] += delta


def observe_histogram(name: str, value: float) -> None:
    if value < 0:
        value = 0.0
    with _histogram_lock:
        h = _histograms[name]
        h["count"] += 1.0
        h["sum"] += value
        if value > h.get("max", 0.0):
            h["max"] = value
        for b in _DEFAULT_BUCKETS:
            key = f"le_{b}"
            if value <= b:
                h[key] = h.get(key, 0.0) + 1.0


def snapshot() -> dict:
    with _counter_lock:
        counters = dict(_counters)
    with _gauge_lock:
        gauges = dict(_gauges)
    with _histogram_lock:
        histograms = {k: dict(v) for k, v in _histograms.items()}

    return {
        "generated_at": time.time(),
        "counters": counters,
        "gauges": gauges,
        "histograms": histograms,
    }

def _sanitize_role(role: str) -> str:
    cleaned = []
    for ch in (role or "").strip():
        if ch.isalnum() or ch in {"-", "_"}:
            cleaned.append(ch)
        else:
            cleaned.append("_")
    out = "".join(cleaned).strip("_") or "proc"
    return out[:40]


def _export_file_path(export_dir: str, role: str) -> str:
    safe_role = _sanitize_role(role)
    pid = os.getpid()
    return os.path.join(export_dir, f"metrics-{safe_role}-{pid}.json")


def _write_json_atomic(path: str, payload: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, path)


async def _export_loop_fn(path: str, interval_seconds: float, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            _write_json_atomic(path, snapshot())
        except Exception:
            # Best-effort export; metrics must never crash the process.
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            continue


async def start_metrics_exporter(
    *,
    role: str,
    export_dir: str,
    interval_seconds: float = 5.0,
) -> None:
    """Periodically export an in-process snapshot to disk for cross-worker aggregation."""
    if not export_dir:
        return
    if interval_seconds <= 0:
        interval_seconds = 5.0

    global _export_task, _export_stop_event, _export_loop, _export_path
    loop = asyncio.get_running_loop()
    if _export_task and not _export_task.done() and _export_loop is loop:
        return

    try:
        os.makedirs(export_dir, exist_ok=True)
    except Exception:
        return
    _export_loop = loop
    _export_stop_event = asyncio.Event()
    _export_path = _export_file_path(export_dir, role)
    _export_task = asyncio.create_task(
        _export_loop_fn(_export_path, interval_seconds, _export_stop_event)
    )


async def stop_metrics_exporter() -> None:
    global _export_task, _export_stop_event, _export_loop, _export_path
    if _export_stop_event:
        _export_stop_event.set()
    if _export_task:
        await asyncio.gather(_export_task, return_exceptions=True)
    _export_task = None
    _export_stop_event = None
    _export_loop = None
    if _export_path:
        try:
            os.remove(_export_path)
        except FileNotFoundError:
            pass
        except Exception:
            pass
    _export_path = None


def _load_snapshot_file(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if "generated_at" not in data:
        return None
    return data


def _merge_snapshots(snapshots: list[dict]) -> dict:
    merged_counters: dict[str, float] = {}
    merged_gauges: dict[str, float] = {}
    merged_histograms: dict[str, dict[str, float]] = {}

    for snap in snapshots:
        counters = snap.get("counters") or {}
        if isinstance(counters, dict):
            for name, value in counters.items():
                try:
                    merged_counters[name] = merged_counters.get(name, 0.0) + float(value)
                except Exception:
                    continue

        gauges = snap.get("gauges") or {}
        if isinstance(gauges, dict):
            for name, value in gauges.items():
                try:
                    merged_gauges[name] = merged_gauges.get(name, 0.0) + float(value)
                except Exception:
                    continue

        histograms = snap.get("histograms") or {}
        if isinstance(histograms, dict):
            for name, h in histograms.items():
                if not isinstance(h, dict):
                    continue
                out = merged_histograms.setdefault(name, {"count": 0.0, "sum": 0.0, "max": 0.0})
                try:
                    out["count"] += float(h.get("count", 0.0))
                    out["sum"] += float(h.get("sum", 0.0))
                    out["max"] = max(out["max"], float(h.get("max", 0.0)))
                    for b in _DEFAULT_BUCKETS:
                        key = f"le_{b}"
                        out[key] = out.get(key, 0.0) + float(h.get(key, 0.0))
                except Exception:
                    continue

    return {
        "generated_at": time.time(),
        "counters": merged_counters,
        "gauges": merged_gauges,
        "histograms": merged_histograms,
    }


def snapshot_multiprocess(
    export_dir: str,
    *,
    role: str,
    max_age_seconds: float = 30.0,
) -> dict:
    """Aggregate metrics across worker processes that export to `export_dir`."""
    # Always include a fresh in-process snapshot for the current worker.
    snapshots = [snapshot()]
    if not export_dir:
        return snapshots[0]
    try:
        entries = list(os.scandir(export_dir))
    except Exception:
        return snapshots[0]

    now = time.time()
    pid_suffix = f"-{os.getpid()}.json"
    safe_role = _sanitize_role(role)
    prefix = f"metrics-{safe_role}-"

    for entry in entries:
        try:
            if not entry.is_file():
                continue
            name = entry.name
            if not name.startswith(prefix) or not name.endswith(".json"):
                continue
            if name.endswith(pid_suffix):
                # Avoid double-counting our own process (we already added snapshot()).
                continue
            snap = _load_snapshot_file(entry.path)
            if not snap:
                continue
            generated_at = float(snap.get("generated_at", 0.0))
            if max_age_seconds > 0 and (now - generated_at) > max_age_seconds:
                continue
            snapshots.append(snap)
        except Exception:
            continue

    return _merge_snapshots(snapshots)


def render_prometheus(
    *,
    export_dir: str | None = None,
    role: str = "proc",
    max_age_seconds: float = 30.0,
) -> str:
    snap = (
        snapshot_multiprocess(export_dir, role=role, max_age_seconds=max_age_seconds)
        if export_dir
        else snapshot()
    )
    lines: list[str] = []

    for name, value in sorted(snap["counters"].items()):
        lines.append(f"# TYPE {name} counter")
        lines.append(f"{name} {value}")

    for name, value in sorted(snap["gauges"].items()):
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")

    for name, h in sorted(snap["histograms"].items()):
        count = h["count"]
        total = h["sum"]
        max_value = h.get("max", 0.0)
        avg = (total / count) if count else 0.0
        lines.append(f"# TYPE {name} histogram")
        for b in _DEFAULT_BUCKETS:
            key = f"le_{b}"
            lines.append(f'{name}_bucket{{le="{b}"}} {h.get(key, 0.0)}')
        lines.append(f'{name}_bucket{{le="+Inf"}} {count}')
        lines.append(f"{name}_count {count}")
        lines.append(f"{name}_sum {total}")
        lines.append(f"# TYPE {name}_max gauge")
        lines.append(f"{name}_max {max_value}")
        lines.append(f"# TYPE {name}_avg gauge")
        lines.append(f"{name}_avg {avg}")

    return "\n".join(lines) + "\n"
