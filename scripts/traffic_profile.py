#!/usr/bin/env python3
"""Appendix C traffic-profile runner.

Replays the challenge's exact daily schedule against a local API::

    00:00-08:00  10 calls/min            (baseline)
    08:00-10:00  ramp 10 -> 50 over 15 min, then hold 50
    10:00-14:00  10 calls/min            (baseline)
    14:00-16:00  ramp 10 -> 50 over 15 min, then hold 50   (second campaign spike)
    16:00-24:00  10 calls/min            (baseline)

Design notes
------------
* ``rate_calls_per_minute`` is the pure schedule function used by the unit tests.
* Scheduling uses ``time.monotonic`` (via the event loop clock) and a
  linear-interpolated ramp.
* Concurrency is bounded by ``--max-concurrency`` and the launch queue is capped,
  so memory never grows without bound; if the service cannot keep up the
  shortfall is reported rather than silently buffered.
* ``--speed`` scales simulated time (``--speed 60`` => 1 simulated hour = 1 real
  minute, as suggested by the challenge). ``--demo`` is a short, volume-scaled
  preset that still traverses both campaign spikes.

A short accelerated run is a *demo of the schedule shape*, not a literal
24-hour load test — the summary prints how much was simulated so this is never
ambiguous.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

HOUR = 3600
MINUTE = 60
DAY_SECONDS = 24 * HOUR
RAMP_SECONDS = 15 * MINUTE

BASELINE_RATE = 10.0  # calls/min
PEAK_RATE = 50.0      # calls/min

TICK_REAL_SECONDS = 0.05
MAX_PENDING_LAUNCHES = 400.0

BORROWER_BUCKETS = ("0-30 DPD", "31-60 DPD", "61-90 DPD", "90+ DPD")
PRODUCT_TYPES = ("credit_card", "bnpl", "personal_loan")
# Appendix A suggests roughly a 60/20/15/5 split favouring connected/no_answer.
OUTCOMES = ("connected", "no_answer", "ptp_taken", "dispute")
OUTCOME_WEIGHTS = (0.60, 0.20, 0.15, 0.05)


@dataclass(frozen=True)
class Window:
    start: float
    end: float
    kind: str                       # "flat" or "ramp_hold"
    rate: float = 0.0               # for "flat"
    start_rate: float = 0.0         # for "ramp_hold"
    peak_rate: float = 0.0          # for "ramp_hold"
    ramp_seconds: float = 0.0       # for "ramp_hold"

    def rate_at(self, t: float) -> float:
        if self.kind == "flat":
            return self.rate
        into = t - self.start
        if into < self.ramp_seconds:
            frac = into / self.ramp_seconds
            return self.start_rate + (self.peak_rate - self.start_rate) * frac
        return self.peak_rate


def _ramp(start: float, end: float) -> Window:
    return Window(
        start=start, end=end, kind="ramp_hold",
        start_rate=BASELINE_RATE, peak_rate=PEAK_RATE, ramp_seconds=RAMP_SECONDS,
    )


SCHEDULE = (
    Window(start=0 * HOUR, end=8 * HOUR, kind="flat", rate=BASELINE_RATE),
    _ramp(8 * HOUR, 10 * HOUR),
    Window(start=10 * HOUR, end=14 * HOUR, kind="flat", rate=BASELINE_RATE),
    _ramp(14 * HOUR, 16 * HOUR),
    Window(start=16 * HOUR, end=24 * HOUR, kind="flat", rate=BASELINE_RATE),
)


def rate_calls_per_minute(t_seconds: float) -> float:
    """Target call rate (calls/minute) at simulated second ``t_seconds``.

    Wraps at the end of the day so a multi-day run repeats the schedule.
    """
    t = t_seconds % DAY_SECONDS
    for window in SCHEDULE:
        if window.start <= t < window.end:
            return window.rate_at(t)
    return BASELINE_RATE


def expected_calls(duration_seconds: float, step_seconds: float) -> float:
    """Integrate the schedule over ``duration_seconds`` in ``step_seconds`` steps."""
    total = 0.0
    steps = max(1, int(duration_seconds / step_seconds))
    for i in range(steps):
        t = i * step_seconds
        total += rate_calls_per_minute(t) / MINUTE * step_seconds
    return total


def _percentile(values, pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[k]


def make_event(rng: random.Random, event_id: Optional[str] = None) -> dict:
    """Build one valid Appendix A call event with a fresh UUID."""
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "campaign_id": "CAMP-2026-09-A",
        "borrower_bucket": rng.choice(BORROWER_BUCKETS),
        "product_type": rng.choice(PRODUCT_TYPES),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "call_duration_s": rng.randint(0, 180),
        "outcome": rng.choices(OUTCOMES, weights=OUTCOME_WEIGHTS, k=1)[0],
        "retry_count": 0,
    }


@dataclass
class Summary:
    planned: int = 0
    expected: int = 0
    launched: int = 0
    completed: int = 0
    ok: int = 0
    failed: int = 0
    retries: int = 0
    latencies: list = field(default_factory=list)
    rejections: int = 0
    shortfall: float = 0.0
    network_errors: int = 0


async def _send_one(client: httpx.AsyncClient, payload: dict, summary: Summary) -> None:
    started = time.perf_counter()
    try:
        response = await client.post("/calls", json=payload)
    except Exception:  # noqa: BLE001 - browser/network-level failure
        summary.network_errors += 1
        summary.failed += 1
        return
    summary.latencies.append((time.perf_counter() - started) * 1000.0)
    summary.completed += 1
    if response.status_code == 200:
        summary.ok += 1
        body = response.json()
        if isinstance(body.get("retry_count"), int):
            summary.retries += body["retry_count"]
    else:
        summary.failed += 1
        if response.status_code == 503:
            summary.rejections += 1


async def run(args, client_factory=httpx.AsyncClient) -> Summary:
    rng = random.Random(args.seed)
    summary = Summary()

    sim_seconds = args.duration_hours * HOUR
    dt_sim = TICK_REAL_SECONDS * args.speed
    ticks = max(1, int(sim_seconds / dt_sim))
    expected = expected_calls(sim_seconds, dt_sim) * args.volume_scale

    inflight: set = set()
    owed = 0.0
    sim_t = 0.0

    async with client_factory(base_url=args.base_url, timeout=args.timeout) as client:
        try:
            for _ in range(ticks):
                owed += (
                    rate_calls_per_minute(sim_t) / MINUTE * dt_sim * args.volume_scale
                )
                # Never buffer an unbounded backlog: cap it and record the gap.
                if owed > MAX_PENDING_LAUNCHES:
                    summary.shortfall += owed - MAX_PENDING_LAUNCHES
                    owed = MAX_PENDING_LAUNCHES

                while owed >= 1.0:
                    while len(inflight) >= args.max_concurrency:
                        done, inflight = await asyncio.wait(
                            inflight, return_when=asyncio.FIRST_COMPLETED
                        )
                        for task in done:
                            task.result()
                    owed -= 1.0
                    summary.planned += 1
                    summary.launched += 1
                    inflight.add(
                        asyncio.create_task(
                            _send_one(client, make_event(rng), summary)
                        )
                    )
                await asyncio.sleep(TICK_REAL_SECONDS)
                sim_t += dt_sim
        finally:
            for task in inflight:
                task.cancel()
            if inflight:
                await asyncio.gather(*inflight, return_exceptions=True)

    summary.expected = round(expected)
    return summary


def _print_summary(args, summary: Summary) -> dict:
    sim_minutes = (args.duration_hours * HOUR) / MINUTE
    planned_avg = (summary.expected / sim_minutes) if sim_minutes else 0.0
    actual_avg = (summary.launched / sim_minutes) if sim_minutes else 0.0
    sustained = actual_avg >= 0.95 * planned_avg if planned_avg else True

    lat = summary.latencies
    report = {
        "simulated_hours": args.duration_hours,
        "speed_sim_seconds_per_real_second": args.speed,
        "volume_scale": args.volume_scale,
        "max_concurrency": args.max_concurrency,
        "planned_requests": summary.expected,
        "launched_requests": summary.launched,
        "completed_requests": summary.completed,
        "succeeded": summary.ok,
        "failed": summary.failed,
        "dead_lettered_503": summary.rejections,
        "network_errors": summary.network_errors,
        "retries_performed": summary.retries,
        "planned_rate_calls_per_sim_min": round(planned_avg, 3),
        "actual_rate_calls_per_sim_min": round(actual_avg, 3),
        "rate_sustained": sustained,
        "shortfall_unlaunched": round(summary.shortfall, 2),
        "latency_ms": {
            "p50": _round(_percentile(lat, 50)),
            "p90": _round(_percentile(lat, 90)),
            "p99": _round(_percentile(lat, 99)),
        },
    }
    print(json.dumps(report, indent=2))
    if not sustained:
        print(
            "\nWARNING: the requested rate was NOT fully sustained "
            f"({report['actual_rate_calls_per_sim_min']} vs planned "
            f"{report['planned_rate_calls_per_sim_min']} calls/sim-min). "
            "Increase --max-concurrency or lower --volume-scale.",
            file=sys.stderr,
        )
    return report


def _round(value):
    return round(value, 1) if value is not None else None


async def _run_and_report(args) -> dict:
    summary = await run(args)
    report = _print_summary(args, summary)

    # Final DLQ depth (best effort).
    try:
        async with httpx.AsyncClient(base_url=args.base_url, timeout=args.timeout) as client:
            dlq = (await client.get("/dlq")).json()
            print(f"\nfinal_dlq_depth: {len(dlq.get('items', []))}")
    except Exception as exc:  # noqa: BLE001
        print(f"\nfinal_dlq_depth: unavailable ({type(exc).__name__})")
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Appendix C traffic-profile runner (accelerated simulation)."
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--speed", type=float, default=60.0,
        help="Simulated seconds per real second (60 => 1 sim hour = 1 real minute).",
    )
    parser.add_argument(
        "--duration-hours", type=float, default=24.0,
        help="Simulated hours to run (>=24 repeats the daily schedule).",
    )
    parser.add_argument(
        "--volume-scale", type=float, default=1.0,
        help="Multiply the planned rate (use a small value for laptop demos).",
    )
    parser.add_argument("--max-concurrency", type=int, default=50)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument(
        "--demo", action="store_true",
        help="Short preset: 1 sim day in ~24s, 2%% volume, both campaign spikes.",
    )
    return parser


def _apply_demo_preset(args) -> None:
    """Short, volume-scaled preset used by ``--demo`` (both campaign spikes)."""
    args.speed = 3600.0
    args.duration_hours = 24.0
    args.volume_scale = 0.02
    args.max_concurrency = min(args.max_concurrency, 20)


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    if args.demo:
        _apply_demo_preset(args)
        print(
            "DEMO MODE: compressed, volume-scaled schedule (both campaign spikes). "
            "This is not a literal 24-hour load test.\n"
        )
    if args.volume_scale <= 0 or args.speed <= 0:
        print("--volume-scale and --speed must be positive", file=sys.stderr)
        return 2
    try:
        asyncio.run(_run_and_report(args))
    except KeyboardInterrupt:
        print("\ninterrupted; partial results above (tasks cancelled cleanly)")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
