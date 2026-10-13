"""Request/response schemas for the Collections Inference Service.

The call-event schema mirrors Appendix A of the challenge exactly:

Required: ``event_id`` (UUID), ``campaign_id``, ``borrower_bucket``,
``timestamp`` (date-time), ``outcome``.
Optional: ``product_type``, ``call_duration_s``, ``retry_count``.
"""
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

BorrowerBucket = Literal["0-30 DPD", "31-60 DPD", "61-90 DPD", "90+ DPD"]
ProductType = Literal["credit_card", "bnpl", "personal_loan"]
Outcome = Literal["connected", "no_answer", "ptp_taken", "dispute", "failed_downstream"]

# Deterministic scenarios supported by the mock dependency's test mode. These
# are only honored when ENABLE_TEST_CONTROLS is true on both services.
TEST_SCENARIOS = (
    "always_succeed",
    "fail_once_then_succeed",
    "always_fail",
    "slow",
)


class CallEvent(BaseModel):
    """A single synthetic collections call event (Appendix A).

    ``event_id`` is parsed as a UUID so malformed identifiers are rejected with
    HTTP 422. The remaining required fields follow the challenge's JSON Schema;
    ``product_type``, ``call_duration_s`` and ``retry_count`` are optional.
    """

    # Required by Appendix A.
    event_id: UUID
    campaign_id: str
    borrower_bucket: BorrowerBucket
    timestamp: datetime
    outcome: Outcome

    # Optional by Appendix A.
    product_type: Optional[ProductType] = None
    call_duration_s: Optional[int] = Field(default=None, ge=0)
    retry_count: Optional[int] = Field(default=None, ge=0)


class PipelineRequest(BaseModel):
    """Internal request sent to the model dependency boundary."""

    event_id: str
    audio_ref: str = "synthetic"
    # Optional test-only scenario forwarded to the mock dependency. Only honored
    # when test controls are enabled on both the API and the mock.
    scenario: Optional[str] = None
