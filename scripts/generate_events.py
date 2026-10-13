#!/usr/bin/env python3
"""Generate synthetic Appendix A call events as JSON lines.

Mirrors the challenge's guidance: randomize the timestamp across the campaign
window, sample ``borrower_bucket`` / ``product_type`` / ``outcome`` with roughly
a 60/20/15/5 split favouring connected/no_answer, and set
``outcome="failed_downstream"`` on ~20% of events to match the injected failure
rate in Appendix B.

All ids are freshly generated UUIDs and all data is synthetic — never real
borrower information.

Usage::

    python scripts/generate_events.py --count 500 > events.jsonl
"""
from __future__ import annotations

import argparse
import json
import random
import uuid
from datetime import datetime, timedelta, timezone

BUCKETS = ["0-30 DPD", "31-60 DPD", "61-90 DPD", "90+ DPD"]
PRODUCTS = ["credit_card", "bnpl", "personal_loan"]
OUTCOMES = ["connected", "no_answer", "ptp_taken", "dispute"]
OUTCOME_WEIGHTS = [0.60, 0.20, 0.15, 0.05]

CAMPAIGN_START = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)
CAMPAIGN_SECONDS = 8 * 3600  # campaign window for timestamp randomisation


def make_event(rng: random.Random) -> dict:
    timestamp = CAMPAIGN_START + timedelta(seconds=rng.randint(0, CAMPAIGN_SECONDS))
    # ~20% of events represent the injected downstream failure.
    if rng.random() < 0.20:
        outcome = "failed_downstream"
    else:
        outcome = rng.choices(OUTCOMES, weights=OUTCOME_WEIGHTS, k=1)[0]
    return {
        "event_id": str(uuid.uuid4()),
        "campaign_id": "CAMP-2026-09-A",
        "borrower_bucket": rng.choice(BUCKETS),
        "product_type": rng.choice(PRODUCTS),
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "call_duration_s": rng.randint(0, 300) if outcome not in {"no_answer"} else 0,
        "outcome": outcome,
        "retry_count": rng.randint(0, 3),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate synthetic Appendix A events.")
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)
    for _ in range(max(0, args.count)):
        print(json.dumps(make_event(rng)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
