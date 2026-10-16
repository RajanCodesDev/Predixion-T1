import os


def _as_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


MOCK_URL = os.getenv("MOCK_URL", "http://mock:8001")
DATABASE_PATH = os.getenv("DATABASE_PATH", "/data/dlq.sqlite3")
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))
BASE_DELAY_SECONDS = float(os.getenv("BASE_DELAY_SECONDS", "0.2"))
MAX_DELAY_SECONDS = float(os.getenv("MAX_DELAY_SECONDS", "2"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "3"))
CB_FAILURE_THRESHOLD = int(os.getenv("CB_FAILURE_THRESHOLD", "5"))
CB_RECOVERY_SECONDS = float(os.getenv("CB_RECOVERY_SECONDS", "10"))

# Test-only controls (failure simulation / circuit reset). Disabled by default
# and must never be enabled on a public/production deployment.
# SET THIS TO true ONLY FOR LOCAL DEMOS AND DEMONSTRATIONS.
ENABLE_TEST_CONTROLS = _as_bool("ENABLE_TEST_CONTROLS", "false")

# --- CloudWatch custom metrics (Track 2 target tracking + dashboard) --------
# Disabled by default so local runs and the test suite never touch AWS. The ECS
# API task sets all of these in infra/terraform/main.tf.
CLOUDWATCH_METRICS_ENABLED = _as_bool("CLOUDWATCH_METRICS_ENABLED", "false")
CLOUDWATCH_METRIC_NAMESPACE = os.getenv(
    "CLOUDWATCH_METRIC_NAMESPACE", "CollectionsChallenge"
)
# Flush cadence for the in-flight gauge / counters / latency samples. Kept at or
# above one second so a misconfiguration cannot hammer the PutMetricData API.
CLOUDWATCH_METRICS_INTERVAL = max(1.0, float(os.getenv("CLOUDWATCH_METRICS_INTERVAL", "10")))
# Bounded dimensions: exactly the names the Terraform target-tracking policy and
# dashboard query. Empty values simply omit the dimension (local development).
ECS_CLUSTER_NAME = os.getenv("ECS_CLUSTER_NAME", "")
ECS_SERVICE_NAME = os.getenv("ECS_SERVICE_NAME", "")

# --- Dead-letter queue backend ---------------------------------------------
# "sqlite" (default) keeps the original per-task file at DATABASE_PATH; it is
# the only backend that works without AWS and is what local runs/tests use.
# "dynamodb" uses a single shared table so dead letters survive task
# replacement and are visible to every task behind the load balancer. Terraform
# selects "dynamodb" for the ECS API task and sets DLQ_TABLE_NAME.
DLQ_BACKEND = os.getenv("DLQ_BACKEND", "sqlite").strip().lower()
DLQ_TABLE_NAME = os.getenv("DLQ_TABLE_NAME", "").strip()
