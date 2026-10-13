# Collections Inference Service — Usage & Operations Guide

This is the operational guide for the service: how to run it, every endpoint it
exposes, how to observe and reproduce failures, and how to run the test suite.

There are **two independent services** in this repository. Keep them separate in
your head (and in your docs):

| Service | Purpose | Default port | Docker Compose service | Container |
|---|---|---|---|---|
| **api** | Collections inference API + DLQ + dashboard | `8000` | `api` | `collections-agent-track1-api-1` |
| **mock** | Simulated STT → LLM → TTS dependency | `8001` | `mock` | `collections-agent-track1-mock-1` |

The api talks to the mock over HTTP at `MOCK_URL` (default `http://mock:8001`).

---

## 1. Requirements

- Docker Engine + Docker Compose plugin (recommended path), **or**
- Python 3.12+ for local development/tests. Dependencies live in `requirements.txt`:
  `fastapi`, `uvicorn[standard]`, `httpx`, `pydantic`, `pytest`, `pytest-asyncio`,
  `pytest-cov`.

No credentials, secrets, cloud accounts, or paid services are required.

---

## 2. Quick start

```bash
# From the repository root
docker compose up --build
```

Endpoints once healthy:

- API health: <http://localhost:8000/health>
- **Dashboard: <http://localhost:8000/ui>**
- Swagger UI / OpenAPI: <http://localhost:8000/docs> (spec at `/openapi.json`, ReDoc at `/redoc`)
- Mock health: <http://localhost:8001/health>

To enable the dashboard's deterministic failure-simulation controls for a local
demo, start the stack with test controls on (both services):

```bash
ENABLE_TEST_CONTROLS=true docker compose up --build
```

Stop the stack (your DLQ data on the host is untouched):

```bash
docker compose down
```

> ℹ️ The DLQ is a **bind mount** on the host at `./data/dlq.sqlite3`, not a
> Docker named volume, so `docker compose down -v` does **not** delete it. Your
> data only goes away if you delete the host file yourself.

---

## 3. Configuration

All tunables are read from environment variables at process start.

| Variable | Default | Service(s) | Meaning |
|---|---|---|---|
| `MOCK_URL` | `http://mock:8001` | api | Base URL of the mock dependency |
| `DATABASE_PATH` | `/data/dlq.sqlite3` | api | SQLite DLQ file location |
| `MAX_ATTEMPTS` | `3` | api | Total HTTP attempts per logical event |
| `BASE_DELAY_SECONDS` | `0.2` | api | First retry backoff delay |
| `MAX_DELAY_SECONDS` | `2` | api | Backoff ceiling |
| `REQUEST_TIMEOUT_SECONDS` | `3` | api | Per-request HTTP timeout to the mock |
| `CB_FAILURE_THRESHOLD` | `5` | api | Failed logical events before the circuit opens |
| `CB_RECOVERY_SECONDS` | `10` | api | Cooldown before a half-open probe |
| `ENABLE_TEST_CONTROLS` | `false` | api + mock | Enables test-only failure simulation (**keep false in production**) |
| `DLQ_HOST_DIR` | `./data` | docker compose | Host directory bind-mounted at `/data` for the SQLite DLQ |

See `.env.example`. `docker-compose.yml` uses `${VAR:-default}` interpolation for
every setting, so a shell or `.env` value always overrides the built-in default.

---

## 4. API service endpoints

Base URL: `http://localhost:8000`. JSON in, JSON out. No authentication.

### 4.1 `GET /health`

- **Purpose:** liveness + current circuit-breaker state.
- **Headers:** none.
- **Response `200`:**
  ```json
  { "status": "ok", "circuit_state": "closed" }
  ```
- `circuit_state` is one of `closed`, `open`, `half_open` (or `starting` before startup completes).
- **DLQ/circuit:** read-only; reflects in-process breaker state.

```bash
curl -s http://localhost:8000/health
```

### 4.2 `GET /dependencies`

- **Purpose:** aggregate health used by the dashboard.
- **Response `200`:**
  ```json
  {
    "api": { "status": "ok", "circuit_state": "closed" },
    "mock": { "reachable": true, "status_code": 200, "status": "ok" }
  }
  ```
- If the mock is unreachable: `"mock": { "reachable": false, "error": "ConnectError: ..." }`.
- If the api has no HTTP client configured (only in some test setups): `"reachable": null`.

```bash
curl -s http://localhost:8000/dependencies
```

### 4.3 `POST /calls`

- **Purpose:** process one synthetic call event through the resilient pipeline.
- **Headers:** `Content-Type: application/json`; optional `X-Test-Scenario` (see §7).
- **Request schema** (Pydantic `CallEvent`, exactly Appendix A):

  | Field | Type | Required | Notes |
  |---|---|---|---|
  | `event_id` | UUID string | **yes** | Valid RFC-4122 UUID; doubles as DLQ idempotency key |
  | `campaign_id` | string | **yes** | |
  | `borrower_bucket` | enum | **yes** | `0-30 DPD`, `31-60 DPD`, `61-90 DPD`, `90+ DPD` |
  | `timestamp` | ISO date-time | **yes** | Full `/Z` or offset timestamp |
  | `outcome` | enum | **yes** | `connected`, `no_answer`, `ptp_taken`, `dispute`, `failed_downstream` |
  | `product_type` | enum | no | `credit_card`, `bnpl`, `personal_loan` |
  | `call_duration_s` | int ≥ 0 | no | |
  | `retry_count` | int ≥ 0 | no | Accepted but informational |

- **Example request:**
  ```bash
  curl -sS -X POST http://localhost:8000/calls \
    -H 'Content-Type: application/json' \
    -d '{"event_id":"4b7f3d0e-0a2a-4c1f-8d00-4f6b9a1c2e3d","campaign_id":"CAMP-2026-10-A","borrower_bucket":"0-30 DPD","product_type":"bnpl","timestamp":"2026-09-01T09:03:12Z","call_duration_s":0,"outcome":"connected","retry_count":0}'
  ```
- **Success `200`:**
  ```json
  {
    "event_id": "4b7f3d0e-0a2a-4c1f-8d00-4f6b9a1c2e3d",
    "outcome": "connected",
    "retry_count": 0,
    "pipeline": {
      "status": "ok",
      "transcript": "synthetic transcript",
      "response_audio_ref": "synthetic",
      "latency_ms": 298
    }
  }
  ```
  `retry_count` = additional attempts after the first (0 = first attempt succeeded).
- **Failure `503` (downstream exhausted):**
  ```json
  { "detail": { "event_id": "4b7f3d0e-0a2a-4c1f-8d00-4f6b9a1c2e3d", "status": "dead_lettered", "reason": "HTTPStatusError: 503 Service Unavailable" } }
  ```
- **Failure `503` (circuit open):**
  ```json
  { "detail": { "event_id": "4b7f3d0e-0a2a-4c1f-8d00-4f6b9a1c2e3d", "status": "dead_lettered", "reason": "circuit_open" } }
  ```
- **Failure `422`:** request body failed validation (missing/invalid fields). No downstream call and no DLQ write.
- **Status codes:** `200`, `422`, `503`.
- **Retry:** up to `MAX_ATTEMPTS` total attempts (e.g. 3 = one initial + two retries) with jittered exponential backoff (`min(BASE*2^n, MAX) × [0.8,1.2]`). HTTP 5xx, timeouts and transport errors are retried; HTTP 4xx and schema errors are **not** retried (`NonRetryableError`).
- **Circuit:** each exhausted **retryable** logical event counts as one failure toward `CB_FAILURE_THRESHOLD`; permanent errors do not trip the breaker.
- **DLQ:** every `503` writes/preserves the event in SQLite. `retry_count` stored is the **actual** number of retries performed (0 for non-retryable / circuit-open, `MAX_ATTEMPTS - 1` when all retryable attempts are exhausted).

### 4.4 `GET /dlq`

- **Purpose:** list dead-lettered events.
- **Response `200`:** `{ "items": [ { …dlq row… } ] }`, ordered by `created_at`.
- Each item contains `event_id`, `payload` (the original event), `failure_reason`, `retry_count`, `status`, `created_at`, `updated_at`.

```bash
curl -s http://localhost:8000/dlq
```

### 4.5 `GET /dlq/{event_id}`

- **Purpose:** fetch one dead-lettered event.
- **Response `200`:** the row object (with parsed `payload`).
- **`404`:** `{"detail":"event_not_found"}`.

```bash
curl -s http://localhost:8000/dlq/4b7f3d0e-0a2a-4c1f-8d00-4f6b9a1c2e3d
```

### 4.6 `POST /dlq/{event_id}/replay`

- **Purpose:** re-run a dead-lettered event through the same resilient pipeline.
- **Headers:** optional `X-Test-Scenario` (see §7).
- **Success `200`:**
  ```json
  { "event_id": "4b7f3d0e-0a2a-4c1f-8d00-4f6b9a1c2e3d", "status": "completed", "retry_count": 0, "pipeline": { "status": "ok", ... } }
  ```
- **`404`:** unknown event → `{"detail":"event_not_found"}`.
- **`409`:** already being processed → `{"detail":"event_already_processing"}` (guard against concurrent replay; the status transition is claimed atomically, so two parallel replays cannot both win).
- **`503`:** replay failed → `{"detail":{"event_id":"4b7f3d0e-0a2a-4c1f-8d00-4f6b9a1c2e3d","status":"pending","reason":"..."}}`. The row is returned to `pending` with the new reason (it never sticks in `processing` on a handled error).
- **Status transitions:** `pending → processing → completed` (success) or `pending → processing → pending` (failure).
- **Circuit:** replay obeys the breaker; an open circuit yields reason `circuit_open` and status back to `pending`.

```bash
curl -s -X POST http://localhost:8000/dlq/4b7f3d0e-0a2a-4c1f-8d00-4f6b9a1c2e3d/replay
```

### 4.7 `GET /ui`

- **Purpose:** the browser-based E2E testing dashboard (see §6).
- **Response `200`:** `text/html`.

### 4.8 `GET /`

- **Purpose:** convenience redirect to `/ui` (`307`).

### 4.9 `GET /test/scenarios` (test controls)

- **Purpose:** report whether test controls are enabled and expose config the UI needs.
- **Response `200`** (available in both modes; safe to call):
  ```json
  {
    "enabled": false,
    "scenarios": ["always_succeed","fail_once_then_succeed","always_fail","slow"],
    "max_attempts": 3,
    "max_delay_seconds": 2.0,
    "circuit_failure_threshold": 5,
    "circuit_recovery_seconds": 10.0
  }
  ```

### 4.10 `POST /test/circuit/reset` (test controls)

- **Purpose:** force the in-process circuit breaker back to `closed` (demo aid).
- **Response `200`:** `{ "circuit_state": "closed" }`.
- **`403`:** `{"detail":"test_controls_disabled"}` when `ENABLE_TEST_CONTROLS` is false.

---

## 5. Mock dependency endpoints

Base URL: `http://localhost:8001`. This service exists only to simulate the
downstream STT/LLM/TTS pipeline.

### 5.1 `GET /health`

- **Response `200`:** `{ "status": "ok", "test_controls": false }`.

### 5.2 `POST /pipeline/process`

- **Purpose:** simulate one STT → LLM → TTS call.
- **Request schema:**

  | Field | Type | Notes |
  |---|---|---|
  | `event_id` | string | required |
  | `audio_ref` | string | default `synthetic` |
  | `scenario` | string? | test-only; only honored when `ENABLE_TEST_CONTROLS=true` |
  | `delay_ms` | int? | test-only; used by the `slow` scenario |

- **Default behaviour** (no scenario): independent **20% HTTP 503** per request — injected *before* any latency — otherwise **lognormal latency** `max(1, int(lognormvariate(5.7, 0.62)))` ms. The challenge's `(5.7, 0.35)` is kept for the median (≈300 ms) but the spread is retuned so measured p90 ≈ 663 ms and p99 ≈ 1278 ms (measured on 200,000 samples — see `scripts/latency_sample.py`). The response's `latency_ms` is the **sampled** value the mock slept for (deterministic), not a wall-clock reading.
- **Success `200`:**
  ```json
  { "status": "ok", "transcript": "synthetic transcript", "response_audio_ref": "synthetic", "latency_ms": 287 }
  ```
- **Failure `503`:**
  ```json
  { "status": "error", "reason": "downstream_timeout" }
  ```
- **Failure `400`:** unknown scenario → `{ "status": "error", "reason": "unknown_scenario" }`.

```bash
curl -s -X POST http://localhost:8001/pipeline/process \
  -H 'Content-Type: application/json' -d '{"event_id":"x","audio_ref":"synthetic"}'
```

> The mock's 20% failure is **per HTTP request**. Because the api retries,
> the per-logical-event failure rate is far lower (roughly `0.2^3 ≈ 0.8%` for
> the default three attempts, assuming independence).

---

## 6. The `/ui` dashboard

Open <http://localhost:8000/ui>. All statistics are **browser-session only**
(they are not persisted backend metrics and are **not** pytest results).

Features:

- **Header:** API reachability, circuit state, mock reachability, test-mode indicator — each with a colored dot **and** a text label (status is not conveyed by color alone).
- **Session statistics:** total, successful, failed, retried, dead-lettered, success rate, average observed latency.
- **Single request:** submit one event (auto-generates a UUID if omitted); shows HTTP status, latency, and the full JSON response (errors shown, not hidden).
- **Batch:** 1–100 events at bounded concurrency (1–20); progress bar and final counts.
- **Failure scenario:** deterministic scenarios (test mode required).
- **DLQ panel:** refresh, select a row to view full payload + failure reason + retry count/status, and replay it.
- **Health checks:** checks `/health`, `/dependencies` and `/test/scenarios`.
- **Session controls:** reset statistics; clear displayed results. Neither deletes backend/DLQ data.

The dashboard distinguishes a **network/browser failure** (fetch rejected) from an
**HTTP failure** (non-2xx with a body).

---

## 7. Test-only failure simulation & safety boundary

`ENABLE_TEST_CONTROLS` defaults to **false**. Only enable it locally.

When enabled on **both** the api and mock:

- The api honors the `X-Test-Scenario` header on `POST /calls` and `POST /dlq/{id}/replay` and forwards the scenario to the mock.
- The mock honors the `scenario` request field.
- `POST /test/circuit/reset` becomes available.

Deterministic scenarios:

| Scenario | Behaviour |
|---|---|
| `always_succeed` | Always `200`, zero latency |
| `fail_once_then_succeed` | First request per `event_id` → `503`, subsequent → `200` |
| `always_fail` | Always `503` (`downstream_timeout`) |
| `slow` | Sleeps `delay_ms` (default 1000 ms, capped at 5000 ms) then `200` |

An unknown scenario returns `400`. When controls are disabled the header/field is
**ignored entirely** — a stray header cannot influence a production request.

**Safety boundary:** these controls can force failures and reset the breaker.
They are gated by a single env flag and documented here as non-production
functionality. The mock's `fail_once_then_succeed` state is keyed by `event_id`,
so unrelated concurrent events never interfere; `slow`/`always_*` are stateless.

---

## 8. Observing and troubleshooting failures

### 8.1 In the dashboard

1. Open `/ui`.
2. Click **Run with scenario → always_fail** (requires test mode) or **Run batch** with the `always_fail` scenario.
3. The **Recent events** table shows the HTTP status, retry count, latency, and reason. A dead-lettered row shows a red `dead-lettered` tag (with text).
4. Scroll to **Dead-letter queue** → **Refresh DLQ**, click the failing `event_id`, and inspect the payload, failure reason, and retry count.
5. Restore success: set the scenario to `always_succeed`, then click **Replay selected**.

### 8.2 With curl

```bash
# Start the stack
docker compose up --build -d

# Health
curl -s http://localhost:8000/health
curl -s http://localhost:8000/dependencies

# Trigger a failure (requires ENABLE_TEST_CONTROLS=true)
curl -s -X POST http://localhost:8000/calls \
  -H 'Content-Type: application/json' \
  -H 'X-Test-Scenario: always_fail' \
  -d '{"event_id":"5c22a1b4-4d33-4e2a-9f10-2b3c4d5e6f70","campaign_id":"CAMP-2026-10-A","borrower_bucket":"0-30 DPD","product_type":"bnpl","timestamp":"2026-09-01T09:03:12Z"}'

# Inspect and replay
curl -s http://localhost:8000/dlq
curl -s http://localhost:8000/dlq/5c22a1b4-4d33-4e2a-9f10-2b3c4d5e6f70
curl -s -X POST http://localhost:8000/dlq/5c22a1b4-4d33-4e2a-9f10-2b3c4d5e6f70/replay \
  -H 'X-Test-Scenario: always_succeed'
```

### 8.3 Distinguishing failure types

Inspect the `detail.reason` in the `503` body (and the `failure_reason` in the DLQ):

| Symptom | `reason` examples | Meaning |
|---|---|---|
| Downstream HTTP error | `HTTPStatusError: 503 Service Unavailable` | The mock (or a real dependency) returned a non-2xx status; the internal URL is deliberately omitted |
| Timeout | `ReadTimeout` / `ReadTimeout: ...` | Exceeded `REQUEST_TIMEOUT_SECONDS` (empty messages become the type name) |
| Connection error | `ConnectError: All connection attempts failed` | The mock is unreachable / not running |
| Circuit open | `circuit_open` | Breaker is open; downstream was **not** called (retry_count 0) |
| Non-retryable (HTTP 4xx) | `HTTPStatusError: 400 Bad Request` | Permanent downstream rejection; **not retried**, still dead-lettered with retry_count 0 |
| Validation error | HTTP `422`, no DLQ row | Request body failed schema validation |

### 8.4 Logs and status

```bash
docker compose ps
docker compose logs --tail=100 api
docker compose logs --tail=100 mock
docker compose logs -f api          # follow
```

`uvicorn` logs each request line (status code) for both services.

### 8.5 Running a single test or a group

```bash
pytest -q                                  # everything
pytest -m unit                             # unit only
pytest -m api                              # API only
pytest -m integration                      # integration only
pytest tests/unit/test_circuit_breaker.py  # one file
pytest tests/api/test_calls.py::test_retry_count_reported   # one test
pytest -k "circuit" -v                     # by keyword
```

---

## 9. Operational details

### Dependencies and startup

- Dependencies in `requirements.txt`. Docker images are `python:3.12-slim`.
- `docker compose up --build` builds and starts `api` and `mock`. `api` declares `depends_on: mock`.

### Local development without Docker

The api defaults `MOCK_URL` to `http://mock:8001` and `DATABASE_PATH` to
`/data/dlq.sqlite3`, neither of which exists on a host. Point them at local
values:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# Terminal 1 — mock
ENABLE_TEST_CONTROLS=true uvicorn mock.main:app --port 8001

# Terminal 2 — api
MOCK_URL=http://localhost:8001 \
DATABASE_PATH=./dlq.sqlite3 \
ENABLE_TEST_CONTROLS=true \
uvicorn app.main:app --port 8000
```

### SQLite persistence and host bind mount

The api stores the DLQ in `DATABASE_PATH` (default `/data/dlq.sqlite3` inside the
container). Compose **bind-mounts the host directory `./data`** at `/data`:

```yaml
volumes:
  - ${DLQ_HOST_DIR:-./data}:/data
```

So the database is directly visible on your machine at
`./data/dlq.sqlite3` (override the host directory with `DLQ_HOST_DIR=/some/path`).
The DLQ **survives container restarts and rebuilds**, and because it is a host
bind mount it is *not* affected by `docker compose down -v`. Writes are upserts
keyed on `event_id`.

The file is created by the container's root user but is world-readable, and an
existing host-owned file keeps its host ownership (root writes it in place). If
permissions ever get confusing, reclaim them on the host:

```bash
sudo chown -R "$USER":"$USER" data
```

Inspect it directly from the host:

```bash
# via the API
curl -s http://localhost:8000/dlq

# or straight from the file
python -c "import sqlite3; c=sqlite3.connect('data/dlq.sqlite3'); print(c.execute('select event_id,status,retry_count from dead_letters').fetchall())"
```

### Rebuilding after code changes

```bash
docker compose up --build
```

`app/static/index.html` is included via `COPY app ./app`, so the dashboard is
rebuilt with the image.

### Resetting the development database safely

The DLQ is only written by this service. Delete the host file and it is
recreated empty on the next write (or restart):

```bash
rm -f data/dlq.sqlite3
```

or from inside the container:

```bash
docker compose exec api rm -f /data/dlq.sqlite3
```

Either way, this only removes the DLQ — no other data is affected.

### OpenAPI / Swagger

- Swagger UI: `/docs`
- ReDoc: `/redoc`
- Machine-readable spec: `/openapi.json`

### Mock latency distribution

The mock uses the challenge's **lognormal** latency shape
(`lognormvariate(5.7, 0.62)` ms): median ≈ 300 ms, measured p90 ≈ 663 ms and
p99 ≈ 1278 ms (200,000 samples, see `scripts/latency_sample.py`). This keeps the
challenge's suggested `mu=5.7` and retunes only the spread `sigma` so the tail
lands near the ~700/1200 ms targets.

### Traffic-profile simulator (Appendix C)

`scripts/traffic_profile.py` replays the exact Appendix C schedule:

```bash
# 1 simulated hour = 1 real minute, full 24h schedule (~23.4k events)
python scripts/traffic_profile.py --speed 60

# Compressed demo: 1 sim day ≈ 24 s at 2% volume, both campaign spikes
python scripts/traffic_profile.py --demo
```

It computes the target rate from the window table (10/min baseline; 08–10 and
14–16 ramps 10→50 over 15 min then hold; back to baseline), uses monotonic time,
bounded concurrency + a capped launch queue, sends unique valid UUID events, and
prints planned vs. actual rates, success/failure, retries, observed-latency
percentiles, and final DLQ depth. See the module docstring for the exact CLI.

### Known limitations

- Circuit-breaker state is **in-process only**; multiple replicas do not share it.
- No distributed exactly-once semantics; `event_id` is the DLQ idempotency key.
- HTTP 4xx / schema errors are not retried; everything else HTTP is (see §4.3).
- `slow` scenario and `delay_ms` are test-only and capped at 5000 ms.
- Dashboard statistics are per browser session, not persisted.
- `scripts/load_test.py` is a quick smoke load test — use
  `scripts/traffic_profile.py` for the Appendix C rate schedule. A `--demo`
  run shows the schedule shape; it is not a literal 24-hour load test.

---

## 10. Testing

```bash
pytest                                   # full suite
pytest -v                                # verbose
pytest -m unit                           # unit tests (fast, no I/O)
pytest -m api                            # API tests
pytest -m integration                    # end-to-end tests
pytest --cov=app --cov-report=term-missing
pytest --cov=app --cov-report=html       # writes htmlcov/index.html
pytest --junitxml=artifacts/junit.xml    # writes JUnit XML
```

Test layout:

```
tests/
├── conftest.py                         # fixtures (temp DB, app factory, mock client)
├── helpers.py                          # deterministic adapters + fake clock + UUID helpers
├── unit/test_resilience.py             # retries, backoff, jitter, retryability, cancellation
├── unit/test_circuit_breaker.py        # breaker state machine
├── unit/test_dlq.py                    # SQLite persistence, claim race, restart survival
├── unit/test_describe_exception.py     # failure-reason formatting / URL hygiene
├── unit/test_traffic_schedule.py       # Appendix C rate/ramp/window calculations
├── unit/test_scripts.py                # traffic runner loop + event generator + demo preset
├── api/test_health.py                  # /health, /dependencies, test controls
├── api/test_calls.py                   # /calls, validation, DLQ, scenarios, non-retryable
├── api/test_dlq_endpoints.py           # /dlq, /dlq/{id}, replay, concurrency (409)
├── api/test_ui.py                      # /ui, /, /docs
├── api/test_app_wiring.py              # factory wiring / lifespan
└── integration/
    ├── test_mock_service.py            # mock scenarios + default behaviour
    ├── test_mock_latency.py            # latency distribution + contract
    └── test_pipeline_e2e.py            # full API→resilience→mock→DLQ flow, restart survival
```

Tests are deterministic: they never use the mock's random 20% path for assertions,
never wait on real backoff delays, and always use a temporary SQLite database.
The suite currently has **138 tests** (`138 passed`) with **99%** coverage of
`app/` and 100% of `mock/`. Running `pytest -m unit`, `-m api`, `-m integration`
splits the suite by layer.

The HTML coverage report is written to `htmlcov/index.html`; open it in a browser.
JUnit XML is written wherever `--junitxml` points (CI uses `artifacts/junit.xml`).
