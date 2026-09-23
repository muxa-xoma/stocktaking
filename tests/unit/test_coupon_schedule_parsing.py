"""Unit tests for MOEX ``bondization`` coupon-schedule parsing (DC-1).

``coupon_schedule_from_block`` is the single place where the MOEX
``coupons`` block shape is fixed into :class:`CouponScheduleEntry`. These
pure unit tests assert the delivered parsing contract: field keys
(``coupondate``/``value_rub``/``value``), the compact-date fallback, the
``value_rub``-over-``value`` preference, and the malformed-row → provider
error semantics (so a shape change is never mistaken for an empty
schedule).
"""

from __future__ import annotations

from datetime import date

import pytest

from bond_accounting.market_data.errors import ProviderUnavailableError
from bond_accounting.market_data.schemas import CouponScheduleEntry, coupon_schedule_from_block


def _row(
    coupondate: str,
    value_rub: object | None = None,
    value: object | None = None,
) -> dict:
    """Build one raw ``coupons`` block row with the delivered lowercase keys."""
    row: dict = {"coupondate": coupondate}
    if value_rub is not None:
        row["value_rub"] = value_rub
    if value is not None:
        row["value"] = value
    return row


def test_parsing_success_returns_entries_in_provider_order() -> None:
    """A well-formed block maps every row to an entry, preserving order."""
    rows = [
        _row("2026-03-08", value_rub=34.04),
        _row("2026-09-08", value_rub=35.4),
        _row("2027-03-08", value_rub=35.4),
    ]

    entries = coupon_schedule_from_block(rows)

    assert entries == [
        CouponScheduleEntry(coupon_date=date(2026, 3, 8), coupon_amount=34.04),
        CouponScheduleEntry(coupon_date=date(2026, 9, 8), coupon_amount=35.4),
        CouponScheduleEntry(coupon_date=date(2027, 3, 8), coupon_amount=35.4),
    ]


def test_parsing_compat_date_yyyymmdd_accepted() -> None:
    """The compact ``YYYYMMDD`` date format is accepted as a fallback."""
    entries = coupon_schedule_from_block([_row("20260908", value_rub=35.4)])

    assert entries == [CouponScheduleEntry(coupon_date=date(2026, 9, 8), coupon_amount=35.4)]


def test_parsing_prefers_value_rub_over_value() -> None:
    """``value_rub`` (RUB-normalized) wins over the per-currency ``value``."""
    entries = coupon_schedule_from_block([_row("2026-03-08", value_rub=35.4, value=35.4)])

    assert entries[0].coupon_amount == 35.4


def test_parsing_falls_back_to_value_when_value_rub_missing() -> None:
    """Without ``value_rub`` the per-currency ``value`` is used."""
    entries = coupon_schedule_from_block([_row("2026-03-08", value=35.4)])

    assert entries == [CouponScheduleEntry(coupon_date=date(2026, 3, 8), coupon_amount=35.4)]


def test_parsing_empty_block_returns_empty_list() -> None:
    """An empty ``coupons`` block is a valid empty schedule, not an error."""
    assert coupon_schedule_from_block([]) == []


def test_parsing_junk_date_raises_provider_error() -> None:
    """A non-date ``coupondate`` is a malformed row → provider error (never [])."""
    with pytest.raises(ProviderUnavailableError):
        coupon_schedule_from_block([_row("not-a-date", value_rub=35.4)])


def test_parsing_missing_amount_raises_provider_error() -> None:
    """A row with neither ``value_rub`` nor ``value`` is a provider error."""
    with pytest.raises(ProviderUnavailableError):
        coupon_schedule_from_block([_row("2026-03-08")])


def test_parsing_non_numeric_amount_raises_provider_error() -> None:
    """A non-numeric amount (junk string) is treated as missing → provider error."""
    with pytest.raises(ProviderUnavailableError):
        coupon_schedule_from_block([_row("2026-03-08", value_rub="abc")])


def test_parsing_missing_date_raises_provider_error() -> None:
    """A row wholly missing ``coupondate`` surfaces as a provider error."""
    with pytest.raises(ProviderUnavailableError):
        coupon_schedule_from_block([{"value_rub": 35.4}])
