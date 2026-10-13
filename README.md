# Collections Agent — Track 1 (Resilience + Dead-Letter + Cost)

Local implementation for the **Predixion AI x TalentX AWS FDE Challenge — Round 1
(Local Track, Open Entry)** — a collections voice-agent inference service that
calls a mock STT → LLM → TTS dependency, plus the resilience layer, dead-letter
handling, model-agnostic adapter boundary, Appendix C traffic simulator, and
Appendix D cost estimates that the track requires.

```
HTTP client: POST /calls ─▶ ResilientPipeline (retry+backoff+circuit breaker)
                                │
                                ▼
                        Pipeline protocol (swappable)
                                │
                                ▼
              Mock STT/LLM/TTS dependency  ◀── 20% 503 / lognormal latency
                                │
              failures ─▶ SQLite DLQ (inspect + replay)
```

## Highlights

| Track 1 requirement | Where |
| --- | --- |
| Retries with backoff + circuit breaker around a mocked dependency | `app/resilience.py` |
| Dead-letter handling: inspectable + replayable persistence | `app/dlq.py`, REST endpoints |
| Model-agnostic dependency boundary | `app/pipeline.py` (`Pipeline` protocol) |
| Six nines-ish failure behavior on a 20% downstream failure rate | end-to-end tests |
| Cost reasoning, two Fargate configurations | [`COST_WRITEUP.md`](COST_WRITEUP.md) |
| Swapping the mock for open-weight models | [`MODEL_SWAP.md`](MODEL_SWAP.md) |
| Appendix C traffic-profile simulator | [`scripts/traffic_profile.py`](scripts/traffic_profile.py) |
| Browser E2E console (optional turnkey demo) | `/ui` (dashboard) |

Detailed docs:
- **[docs/USAGE.md](docs/USAGE.md)** — every endpoint, failure simulation, config
  reference, troubleshooting, and full test commands.
- **[docs/architecture.md](docs/architecture.md)** — module-by-module design.

## Quick start

Requirements: Docker Engine + Compose plugin. Python 3.12+ is only needed to run
tests/scripts on the host.

```bash
docker compose up --build
```

| URL | Purpose |
| --- | --- |
| http://localhost:8000 | API |
| http://localhost:8000/ui | E2E console (dashboard) |
| http://localhost:8000/docs | Swagger |
| http://localhost:8000/health | API health (includes circuit state) |
| http://localhost:8001/health | Mock dependency health |

The dashboard's deterministic failure-simulation controls are **disabled by
default**. For a local demo, start both services with test controls on:

```bash
ENABLE_TEST_CONTROLS=true docker compose up --build
```

> Test controls must stay off on any public/production deployment. See the
> safety boundary in [docs/USAGE.md](docs/USAGE.md#7-test-only-failure-simulation--safety-boundary).

## Submit a synthetic call

```bash
curl -sS -X POST http://localhost:8000/calls \
  -H 'Content-Type: application/json' \
  -d '{
    "event_id":"4b7f3d0e-0a2a-4c1f-8d00-4f6b9a1c2e3d",
    "campaign_id":"CAMP-2026-10-A",
    "borrower_bucket":"0-30 DPD",
    "product_type":"bnpl",
    "timestamp":"2026-09-01T09:03:12Z",
    "call_duration_s":0,
    "outcome":"connected",
    "retry_count":0
  }'
```

The body follows **Appendix A** exactly: `event_id` (UUID), `campaign_id`,
`borrower_bucket`, `timestamp`, `outcome` are **required**; `product_type`,
`call_duration_s`, `retry_count` are **optional**. Invalid enums, malformed
UUIDs/timestamps, and negative durations return HTTP 422.

On success the API returns:

```json
{"event_id":"...","outcome":"connected","retry_count":0,"pipeline":{"status":"ok","transcript":"synthetic transcript","response_audio_ref":"synthetic","latency_ms":298}}
```

The mock follows **Appendix B**: an independent 20% HTTP 503 probability per
request (injected *before* latency) and lognormal successful latency. The
configured `MAX_ATTEMPTS=3` means one initial attempt plus up to two retries,
so the observed final-event failure rate (~1%) is far below the per-request 20%.

### Measured percentile latency (success path only)

Method (see `scripts/latency_sample.py`): sample the exact distribution the
mock uses — `random.lognormvariate(5.7, 0.62)` ms, the challenge's suggested
`(5.7, 0.35)` retuned so the tail matches the ~700/1200 ms targets — over
200,000 samples without sleeping:

| Percentile | Target (Appendix B) | Measured (200k samples) |
| --- | --- | --- |
| p50 | ≈300 ms | **299 ms** |
| p90 | ≈700 ms | **663 ms** |
| p99 | ≈1200 ms | **1278 ms** |

## Inspect and replay the DLQ

```bash
curl -sS http://localhost:8000/dlq                       # list all
curl -sS http://localhost:8000/dlq/<event_id>            # inspect one
curl -sS -X POST http://localhost:8000/dlq/<event_id>/replay
```

- Missing ids → `404 event_not_found`; an event already being replayed → `409
  event_already_processing` (the claim is atomic, so concurrent replays cannot
  race).
- Replay goes through the **same resilient pipeline**. Success → `completed`;
  failure → back to `pending` with the new failure reason.
- The SQLite DB is bind-mounted to the host at `./data/dlq.sqlite3`
  (`DLQ_HOST_DIR` overrides), so events **survive restarts** with the same
  volume.
- Duplicate policy: `event_id` is an idempotency key — re-dead-lettering upserts
  the last payload/reason and resets status to `pending`; a later success does
  **not** auto-clear the row.

## Traffic profile (Appendix C)

```bash
# Full daily schedule, compressed: 1 simulated hour = 1 real minute (~23.4k calls)
python scripts/traffic_profile.py --base-url http://localhost:8000 --speed 60

# Short demo that still traverses both campaign spikes (1 sim day ≈ 24s, 2% volume)
python scripts/traffic_profile.py --base-url http://localhost:8000 --demo
```

The runner implements the exact windows (10/min baseline; 08–10 and 14–16 ramps
10→50 over 15 min then hold; back to 10/min), uses monotonic time, bounded
concurrency with a capped launch queue (no unbounded buffering), unique UUID
events, and reports planned vs. actual rates, success/failures, retries,
latency percentiles, and final DLQ depth. It also supports multi-day runs
(`--duration-hours 48`) and rate scaling (`--volume-scale`) for laptops.

> A `--demo` run demonstrates the schedule **shape**; it is explicitly *not* a
> literal 24-hour load test (see the printed notice and the schedule unit tests).

## Costs and swapping the mock for real open-weight models

- **[COST_WRITEUP.md](COST_WRITEUP.md)** — two Appendix D Fargate configurations
  with a recommendation and assumptions.
- **[MODEL_SWAP.md](MODEL_SWAP.md)** — how to replace the HTTP mock with a real
  Whisper/LLM/TTS stack without touching the resilience layer.

## Configuration and secrets

No secrets, API keys, or credentials are required or stored. Every knob has a
sane default and an environment override (see
[docs/USAGE.md](docs/USAGE.md#1-configuration) for the full table). Docker
Compose uses `${VAR:-default}` interpolation on every setting, so `.env` or
shell values always take effect.

## Tests

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
pytest -v                                   # 138 tests
pytest --cov=app --cov-report=term-missing  # 99% coverage on app/
pytest --junitxml=artifacts/junit.xml
pytest -m unit && pytest -m api && pytest -m integration
```

Current result (this submission): **138 passed**; coverage **99%** for `app/`
(`app/resilience.py`, `app/models.py`, `app/pipeline.py` 100%,
`app/main.py` 99%, `app/dlq.py` 95%) and 100% for `mock/`. CI runs the same
suite without AWS in `.github/workflows/ci.yml`. Tests never depend on the
mock's random 20% failure — they use deterministic injected scenarios and
scripted adapters.

## Design

`ResilientPipeline` depends on a `Pipeline` protocol, not a model vendor.
`MockPipeline` is the HTTP adapter; a real open-weight adapter implements the
same `process(request)` interface while retry/backoff/circuit policy stays in
the resilience layer. See [docs/architecture.md](docs/architecture.md).

## Known limitations (honest)

- Circuit-breaker state is in-process; a multi-replica deployment needs shared
  coordination or instance-local semantics.
- The demo dashboard's session statistics are in-browser only and are explicitly
  not pytest results.
- A compressed traffic run validates the rate schedule's shape; true
  capacity testing needs to account for your hardware/OS as documented.
- All data is fully synthetic. No AWS account is used; all costs are estimates.

## License

Apache-2.0 (see `LICENSE`).