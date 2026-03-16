from datetime import timedelta

import pytest

from time_utils import _parse_offset, db_ago, db_now, parse_db_timestamp, APP_TIMEZONE


def test_parse_offset_valid_values():
    plus = _parse_offset("+03:00")
    minus = _parse_offset("-02:30")

    assert plus.utcoffset(None) == timedelta(hours=3)
    assert minus.utcoffset(None) == timedelta(hours=-2, minutes=-30)


@pytest.mark.parametrize(
    "value",
    [
        "03:00",
        "+3:00",
        "+15:00",
        "+00:99",
        "UTC+3",
        "+14:30",
        "-14:59",
    ],
)
def test_parse_offset_invalid_values(value: str):
    with pytest.raises(RuntimeError):
        _parse_offset(value)


def test_db_ago_is_earlier_than_db_now():
    earlier = db_ago(seconds=5)
    now = db_now()
    assert earlier < now


def test_parse_offset_14_00_is_valid():
    tz = _parse_offset("+14:00")
    assert tz.utcoffset(None) == timedelta(hours=14)


def test_parse_db_timestamp_applies_app_timezone_to_naive_values():
    dt = parse_db_timestamp("2026-02-11 12:34:56")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.utcoffset() == APP_TIMEZONE.utcoffset(None)


def test_parse_db_timestamp_with_aware_input():
    from datetime import timezone
    aware_str = "2026-02-11T12:34:56+05:00"
    dt = parse_db_timestamp(aware_str)
    assert dt is not None
    assert dt.tzinfo is not None
    # Should be converted to APP_TIMEZONE
    assert dt.utcoffset() == APP_TIMEZONE.utcoffset(None)
