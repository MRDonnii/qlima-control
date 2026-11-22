"""Electricity price forecast: parse any common price sensor and rank the price now.

The thermostat raises its target a little in cheap periods and lowers it in
expensive ones. "Cheap" and "expensive" are relative: the current period is
ranked against the known periods from a few hours back to a day ahead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

LEVEL_CHEAP = "cheap"
LEVEL_NORMAL = "normal"
LEVEL_EXPENSIVE = "expensive"
LEVEL_UNKNOWN = "unknown"

# Window the current price is ranked within.
_LOOK_BACK = timedelta(hours=6)
_LOOK_AHEAD = timedelta(hours=24)
# Fewer known periods than this and the ranking means nothing.
_MIN_PERIODS = 8


@dataclass(frozen=True)
class Period:
    start: datetime
    end: datetime
    price: float


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return dt_util.as_utc(value) if value.tzinfo else dt_util.as_utc(value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE))
    if isinstance(value, str):
        parsed = dt_util.parse_datetime(value)
        if parsed is not None:
            return _time(parsed)
    return None


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # drop NaN


def _from_items(items: Any, start_keys, end_keys, price_keys, length: timedelta | None) -> list[Period]:
    out: list[Period] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        start = next((_time(item[k]) for k in start_keys if k in item), None)
        price = next((_float(item[k]) for k in price_keys if k in item), None)
        if start is None or price is None:
            continue
        end = next((_time(item[k]) for k in end_keys if k in item), None)
        if end is None:
            end = start + (length or timedelta(hours=1))
        out.append(Period(start, end, price))
    return out


def parse_prices(attributes: dict[str, Any]) -> list[Period]:
    """Every known price period from a price sensor's attributes, sorted by start.

    Understands Strømligning (``prices``: start/end/price), Nord Pool and
    Energi Data Service (``raw_today``/``raw_tomorrow``: start/end/value or
    hour/price), Tibber-style ``today``/``tomorrow`` lists and a generic
    ``forecast``/``data`` list with start(+end) and price/value.
    """
    periods: list[Period] = []
    starts = ("start", "startsAt", "hour", "time", "from", "datetime")
    ends = ("end", "endsAt", "to")
    values = ("price", "value", "total", "price_per_kwh")
    for key in ("prices", "raw_today", "raw_tomorrow", "today", "tomorrow", "forecast", "data"):
        items = attributes.get(key)
        if isinstance(items, list) and items and not isinstance(items[0], dict):
            continue  # Nord Pool's plain ``today`` list of numbers – raw_today covers it
        periods += _from_items(items, starts, ends, values, None)
    unique: dict[datetime, Period] = {}
    for period in periods:
        unique.setdefault(period.start, period)
    return sorted(unique.values(), key=lambda p: p.start)


def to_hours(periods: list[Period]) -> list[Period]:
    """Average quarter-hour periods into whole hours."""
    buckets: dict[datetime, list[Period]] = {}
    for period in periods:
        hour = period.start.replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hour, []).append(period)
    return [
        Period(hour, hour + timedelta(hours=1), sum(p.price for p in items) / len(items))
        for hour, items in sorted(buckets.items())
    ]


@dataclass(frozen=True)
class PriceRank:
    level: str
    price: float | None
    percentile: float | None  # 0 = cheapest known period, 1 = most expensive
    period_end: datetime | None
    periods: int


def rank_now(
    periods: list[Period],
    now: datetime,
    cheap_share: float = 0.25,
    expensive_share: float = 0.25,
) -> PriceRank:
    """Place the current period among the known ones: cheap, normal or expensive."""
    now = dt_util.as_utc(now)
    current = next((p for p in periods if p.start <= now < p.end), None)
    window = [p for p in periods if now - _LOOK_BACK <= p.start < now + _LOOK_AHEAD]
    if current is None or len(window) < _MIN_PERIODS:
        return PriceRank(LEVEL_UNKNOWN, current.price if current else None, None, current.end if current else None, len(window))
    prices = sorted(p.price for p in window)
    below = sum(1 for price in prices if price < current.price)
    equal = sum(1 for price in prices if price == current.price)
    percentile = (below + 0.5 * equal) / len(prices)
    if percentile <= cheap_share:
        level = LEVEL_CHEAP
    elif percentile >= 1 - expensive_share:
        level = LEVEL_EXPENSIVE
    else:
        level = LEVEL_NORMAL
    return PriceRank(level, current.price, round(percentile, 3), current.end, len(window))
