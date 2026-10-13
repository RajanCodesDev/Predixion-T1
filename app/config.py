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
