#!/usr/bin/env python3
"""Measure the mock's successful-path latency distribution without sleeping.

Samples ``random.lognormvariate(LATENCY_MU, LATENCY_SIGMA)`` (the exact call the
mock uses on its success path) and reports percentiles. This measures only the
*sampled* latency; failure responses are excluded entirely.

Usage::

    python scripts/latency_sample.py --count 100000
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

# Allow `python scripts/latency_sample.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mock.main import LATENCY_MU, LATENCY_SIGMA  # noqa: E402


def percentile(ordered, pct: float) -> float:
    k = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[k]


def measure(count: int, seed: int | None = None) -> dict:
    rng = random.Random(seed)
    samples = sorted(
        max(1, int(rng.lognormvariate(LATENCY_MU, LATENCY_SIGMA))) for _ in range(count)
    )
    return {
        "count": count,
        "mu": LATENCY_MU,
        "sigma": LATENCY_SIGMA,
        "p50_ms": percentile(samples, 50),
        "p90_ms": percentile(samples, 90),
        "p99_ms": percentile(samples, 99),
        "mean_ms": round(sum(samples) / len(samples), 1),
        "min_ms": samples[0],
        "max_ms": samples[-1],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Sample the mock latency distribution.")
    parser.add_argument("--count", type=int, default=100_000)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)
    print(json.dumps(measure(args.count, args.seed), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
