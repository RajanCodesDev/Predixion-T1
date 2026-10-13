"""Appendix C traffic-schedule calculations (`unit`)."""
import pytest

from scripts.traffic_profile import (
    BASELINE_RATE,
    DAY_SECONDS,
    HOUR,
    MINUTE,
    PEAK_RATE,
    SCHEDULE,
    expected_calls,
    make_event,
    rate_calls_per_minute,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "t_seconds, expected",
    [
        (0, 10.0),
        (7 * HOUR + 59 * MINUTE, 10.0),
        (8 * HOUR, 10.0),                       # start of first ramp
        (8 * HOUR + 450, 30.0),                 # halfway through the 15-min ramp
        (8 * HOUR + 900, 50.0),                 # end of ramp -> hold
        (9 * HOUR, 50.0),
        (10 * HOUR, 10.0),                      # back to baseline
        (13 * HOUR, 10.0),
        (14 * HOUR, 10.0),                      # start of second ramp
        (14 * HOUR + 450, 30.0),
        (14 * HOUR + 900, 50.0),
        (15 * HOUR, 50.0),
        (16 * HOUR, 10.0),                      # baseline again
        (23 * HOUR, 10.0),
        (DAY_SECONDS, 10.0),                    # wraps to 00:00
        (26 * HOUR, 10.0),
    ],
)
def test_rate_schedule_and_transitions(t_seconds, expected):
    assert rate_calls_per_minute(t_seconds) == pytest.approx(expected)


def test_ramp_is_monotonic_and_interpolated():
    start = 8 * HOUR
    values = [rate_calls_per_minute(start + s) for s in range(0, 901, 60)]
    assert values[0] == BASELINE_RATE
    assert values[-1] == PEAK_RATE
    assert values == sorted(values)  # monotonically non-decreasing


def test_schedule_windows_cover_the_day_contiguously():
    assert SCHEDULE[0].start == 0
    assert SCHEDULE[-1].end == DAY_SECONDS
    for left, right in zip(SCHEDULE, SCHEDULE[1:]):
        assert left.end == right.start


def test_two_ramp_windows_exist():
    ramps = [w for w in SCHEDULE if w.kind == "ramp_hold"]
    assert len(ramps) == 2
    assert ramps[0].start == 8 * HOUR
    assert ramps[1].start == 14 * HOUR


def test_expected_calls_matches_daily_volume():
    # ~23.4k calls/day under the Appendix C schedule.
    total = expected_calls(DAY_SECONDS, MINUTE)
    assert 23000 < total < 23600


def test_expected_calls_scales_linearly_for_multi_day():
    one_day = expected_calls(DAY_SECONDS, MINUTE)
    two_days = expected_calls(2 * DAY_SECONDS, MINUTE)
    assert two_days == pytest.approx(2 * one_day, rel=1e-6)


def test_make_event_is_appendix_a_valid():
    import random
    import uuid as uuidlib

    from app.models import CallEvent

    event = make_event(random.Random(1))
    # Round-trips through the API schema, proving required fields + UUID.
    parsed = CallEvent.model_validate(event)
    assert isinstance(parsed.event_id, uuidlib.UUID)
    assert parsed.outcome in {
        "connected", "no_answer", "ptp_taken", "dispute", "failed_downstream",
    }
    assert parsed.borrower_bucket in {"0-30 DPD", "31-60 DPD", "61-90 DPD", "90+ DPD"}
