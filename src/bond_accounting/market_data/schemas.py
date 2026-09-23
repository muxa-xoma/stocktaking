"""Bond reference DTO and the MOEX ISS field mapping.

All interpretation of MOEX ISS field names and value formats lives here
(the raw row type and URL/response handling live in
:mod:`bond_accounting.market_data.client`), so a field correction after a
live-request spike touches only this module.

Live-request spike findings (2026-09-23, iss.moex.com) encoded below:

* Column names are UPPERCASE: ``ISIN``, ``SHORTNAME``, ``FACEVALUE``,
  ``COUPONPERCENT``, ``COUPONPERIOD``, ``MATDATE``.
* ``COUPONVALUE`` is the cash coupon amount per period (e.g. ``35.4`` RUB
  on a 1000 RUB face value) — NOT the annual rate. The annual percentage
  rate is ``COUPONPERCENT`` (e.g. ``7.1``), which is what maps to
  ``coupon_rate``.
* ``MATDATE`` is ISO formatted (``2041-05-15``); the compact ``YYYYMMDD``
  variant is accepted as a fallback.
* The issuer title arrives as ``ISSUER`` (merged from the global-search
  row's ``emitent_title`` by the client).
* For securities without an active board row (matured/delisted bonds) the
  client falls back to the description block, which lacks ``COUPONPERIOD``
  and ``COUPONPERCENT``: ``coupon_period_days`` is then derived from
  ``COUPONFREQUENCY`` (coupons per year) and the coupon rate is unknown
  (``0.0``).
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bond_accounting.market_data.errors import ProviderUnavailableError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bond_accounting.market_data.client import MoexBondRaw

logger = logging.getLogger(__name__)

__all__ = [
    "BondReference",
    "CouponScheduleEntry",
    "bond_reference_from_moex",
    "coupon_schedule_from_block",
]

#: Accepted ``MATDATE`` formats (ISO first — the verified live format).
_MATDATE_FORMATS = ("%Y-%m-%d", "%Y%m%d")

#: Days in a (non-leap) year, used to derive the period from coupon frequency.
_DAYS_PER_YEAR = 365

#: Preferred cash-amount column (RUB-normalized) of the MOEX ``bondization``
#: ``coupons`` block, with ``value`` as the per-currency fallback. Both are the
#: cash amount per payment, NOT a percent (the percent is ``valueprc``).
_COUPON_AMOUNT_COLUMNS = ("value_rub", "value")


@dataclass(frozen=True)
class CouponScheduleEntry:
    """One actual coupon payment from the MOEX ``bondization`` schedule.

    Attributes:
        coupon_date: Payment date (``coupondate``).
        coupon_amount: Cash amount per payment in the bond's face unit
            (``value_rub``/``value``) — NOT a percent.
    """

    coupon_date: datetime.date
    coupon_amount: float


def coupon_schedule_from_block(rows: list[dict[str, Any]]) -> list[CouponScheduleEntry]:
    """Map raw ``bondization`` ``coupons`` block rows to schedule entries.

    Column names are UPPERCASE, mirroring :func:`bond_reference_from_moex`:
    the payment date is the ``coupondate`` date column (ISO ``YYYY-MM-DD`` or
    compact ``YYYYMMDD``) and the amount is ``value_rub`` (falling back to
    ``value``), a cash amount per period. Rows are returned in the provider's
    order.

    A malformed row (missing/invalid date or amount) is treated as a provider
    error so a shape change surfaces at exactly one place and is never
    mistaken for an empty schedule.

    Raises:
        ProviderUnavailableError: A required field is missing or invalid.
    """
    entries: list[CouponScheduleEntry] = []
    for row in rows:
        coupon_date = _required_date(row, "coupondate")
        amount: float | None = None
        for column in _COUPON_AMOUNT_COLUMNS:
            parsed = _to_number(row.get(column))
            if parsed is not None:
                amount = parsed
                break
        if amount is None:
            raise ProviderUnavailableError(
                f"MOEX bondization coupon row is missing a cash amount: {row!r}"
            )
        entries.append(CouponScheduleEntry(coupon_date=coupon_date, coupon_amount=amount))
    return entries


@dataclass(frozen=True)
class BondReference:
    """Immutable MOEX bond reference mirroring ``BondCreate`` field-for-field.

    The UI feeds it straight into ``BondCreate(**fields)`` when
    auto-completing the create form; the REST layer serializes it with
    pydantic's ``model_validate`` (``from_attributes``).

    Attributes:
        isin: ISIN code of the bond.
        name: Short ISS name (``SHORTNAME``).
        nominal: Face value as an integer.
        coupon_rate: Annual coupon percentage; ``0.0`` for zero-coupon
            bonds and when the provider does not report a rate.
        coupon_period_days: Calendar days between coupon payments;
            ``0`` = zero-coupon.
        maturity_date: Maturity date.
        issuer: Issuer title when known, else ``None``.
    """

    isin: str
    name: str
    nominal: int
    coupon_rate: float
    coupon_period_days: int
    maturity_date: datetime.date
    issuer: str | None


def bond_reference_from_moex(row: MoexBondRaw) -> BondReference | None:
    """Map one raw ISS row to a :class:`BondReference`.

    Returns ``None`` (logging a warning) when the row cannot be represented
    — e.g. a perpetual bond whose ``MATDATE`` is empty or ``0000-00-00`` —
    so callers can skip it instead of letting one unrepresentable row fail
    the whole search.

    Raises:
        ProviderUnavailableError: The row is malformed — a required field
            is missing or invalid. Per the error contract (spec §5) a
            malformed MOEX response is treated as a provider error.
    """
    maturity_date = _maturity_date(row, "MATDATE")
    if maturity_date is None:
        return None
    return BondReference(
        isin=_required_str(row, "ISIN"),
        name=_required_str(row, "SHORTNAME"),
        nominal=_required_int(row, "FACEVALUE"),
        coupon_rate=_optional_number(row, "COUPONPERCENT", default=0.0),
        coupon_period_days=_coupon_period_days(row),
        maturity_date=maturity_date,
        issuer=_optional_str(row, "ISSUER"),
    )


def _maturity_date(row: MoexBondRaw, key: str) -> datetime.date | None:
    """Read a date field, returning ``None`` for unrepresentable rows.

    A ``MATDATE`` that is not a real calendar date (empty, or the
    perpetual-bond placeholder ``0000-00-00``) cannot be mapped into a
    ``BondReference``; log a warning and return ``None`` so the caller can
    skip the row. A missing value entirely is still a provider error and is
    reported by :func:`_required_date`.
    """
    value = row.get(key)
    if value is None:
        return _required_date(row, key)
    if isinstance(value, str) and not value.strip():
        _warn_unrepresentable(row, key, value)
        return None
    if isinstance(value, str):
        stripped = value.strip()
        for fmt in _MATDATE_FORMATS:
            try:
                parsed = datetime.datetime.strptime(stripped, fmt).date()
            except ValueError:
                continue
            if parsed.year < 1:
                # ``0000-00-00`` (and similar) marks a perpetual bond.
                _warn_unrepresentable(row, key, value)
                return None
            return parsed
        _warn_unrepresentable(row, key, value)
        return None
    return _required_date(row, key)


def _warn_unrepresentable(row: MoexBondRaw, key: str, value: object) -> None:
    """Log a warning for an unrepresentable row and return (skip it)."""
    logger.warning(
        "MOEX row %r has an unrepresentable %r=%r; skipping it",
        row.get("ISIN") or row.get("SECID"),
        key,
        value,
    )


def _required_str(row: MoexBondRaw, key: str) -> str:
    """Read a required non-empty string field."""
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProviderUnavailableError(
            f"MOEX response field {key!r} is missing or not a string: {value!r}"
        )
    return value.strip()


def _optional_str(row: MoexBondRaw, key: str) -> str | None:
    """Read an optional string field (``None`` when missing or empty)."""
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    return str(value)


def _to_number(value: object) -> float | None:
    """Coerce an ISS scalar (number or numeric string) to ``float``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", "."))
        except ValueError:
            return None
    return None


def _required_int(row: MoexBondRaw, key: str) -> int:
    """Read a required numeric field as an ``int`` (e.g. ``FACEVALUE``)."""
    value = _to_number(row.get(key))
    if value is None:
        raise ProviderUnavailableError(
            f"MOEX response field {key!r} is missing or not numeric: {row.get(key)!r}"
        )
    return int(value)


def _optional_number(row: MoexBondRaw, key: str, default: float) -> float:
    """Read an optional numeric field, falling back to ``default``."""
    value = _to_number(row.get(key))
    return default if value is None else value


def _coupon_period_days(row: MoexBondRaw) -> int:
    """Derive ``coupon_period_days`` from the raw row.

    Prefers ``COUPONPERIOD`` (verified for securities with an active board
    row). Otherwise derives it from ``COUPONFREQUENCY`` (coupons per year,
    the only hint available in the description block of matured bonds),
    e.g. 2/year → 182 days; ``0`` when neither is reported (zero-coupon
    semantics).
    """
    period = _to_number(row.get("COUPONPERIOD"))
    if period is not None:
        return int(period)
    frequency = _to_number(row.get("COUPONFREQUENCY"))
    if frequency is not None and frequency > 0:
        return max(1, round(_DAYS_PER_YEAR / frequency))
    return 0


def _required_date(row: Mapping[str, Any], key: str) -> datetime.date:
    """Read a required date field (ISO ``YYYY-MM-DD`` or ``YYYYMMDD``).

    Accepts any ``Mapping[str, Any]`` so both the MOEX ``MoexBondRaw`` rows
    (``bond_reference_from_moex`` path) and the plain ``dict[str, Any]`` rows
    of the ``bondization`` ``coupons`` block (
    :func:`coupon_schedule_from_block`) can pass through the same parser.
    """
    value = row.get(key)
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if not isinstance(value, str):
        raise ProviderUnavailableError(
            f"MOEX response field {key!r} is missing or not a date: {value!r}"
        )
    for fmt in _MATDATE_FORMATS:
        try:
            return datetime.datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    raise ProviderUnavailableError(f"MOEX response field {key!r} is not a date: {value!r}")
