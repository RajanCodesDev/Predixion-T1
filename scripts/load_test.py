#!/usr/bin/env python3
"""Small local smoke load test against POST /calls.

For the exact Appendix C rate schedule use ``scripts/traffic_profile.py``; this
script is just a quick bounded-concurrency smoke check.

Usage::

    BASE_URL=http://localhost:8000 TOTAL_EVENTS=50 CONCURRENCY=5 python scripts/load_test.py
"""
import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone

import httpx

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
TOTAL = int(os.getenv("TOTAL_EVENTS", "50"))
CONCURRENCY = int(os.getenv("CONCURRENCY", "5"))


def _event() -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "campaign_id": "CAMP-2026-09-A",
        "borrower_bucket": "0-30 DPD",
        "product_type": "bnpl",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "call_duration_s": 0,
        "outcome": "connected",
        "retry_count": 0,
    }


async def main() -> None:
    sem = asyncio.Semaphore(CONCURRENCY)
    results = {"ok": 0, "failed": 0}

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        async def send() -> None:
            async with sem:
                try:
                    response = await client.post("/calls", json=_event())
                    results["ok" if response.is_success else "failed"] += 1
                except Exception:  # noqa: BLE001
                    results["failed"] += 1

        started = time.perf_counter()
        await asyncio.gather(*(send() for _ in range(TOTAL)))
        print(
            json.dumps(
                {
                    "total": TOTAL,
                    "duration_seconds": round(time.perf_counter() - started, 2),
                    **results,
                }
            )
        )
        dlq = await client.get("/dlq")
        print("dlq_items:", len(dlq.json().get("items", [])))


if __name__ == "__main__":
    asyncio.run(main())
