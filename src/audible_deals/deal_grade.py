"""Grade a price against the book's own locally recorded price history.

A grade compares one observed price with the observations recorded on earlier
dates only. Entries dated on or after the observation date are ignored, so a
price that is already stored as today's history entry is never compared with
itself, and grading the same price before or after it is recorded gives the
same answer.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from audible_deals.price_history import _as_float, history_key
from audible_deals.product import Product

Grade = Literal["lowest", "good", "typical", "high", "new"]

# Matches the --hist-below filter's five entries; fewer checks are anecdotes.
MIN_CHECKS = 5
# Longer than a typical Audible sale, so the checks cannot all sit inside one.
MIN_SPAN_DAYS = 14
# One check speaks for at most this many days, so a stale check cannot pose as months.
MAX_HOLD_DAYS = 14
# Moves under 10% of the usual price are routine wobble, not a deal or a rise.
MEANINGFUL_CHANGE = 0.10
# Good means the cheaper half of the gap between the lowest and the usual price.
GOOD_RANGE_SHARE = 0.5


@dataclass(frozen=True)
class DealGrade:
    """A grade plus the evidence a surface needs to explain it.

    ``checks`` and ``first_date`` describe the earlier observations the grade
    rests on. The price statistics stay empty for ``new`` so that no surface
    can present numbers derived from too little history.
    """

    grade: Grade
    price: float | None
    observed_on: datetime.date
    checks: int
    first_date: datetime.date | None
    lowest_price: float | None = None
    # The most recent check at ``lowest_price``.
    lowest_date: datetime.date | None = None
    # Day-weighted median of the earlier checks: the usual price.
    typical_price: float | None = None
    # Separate stretches of consecutive checks at or below ``price``.
    times_at_or_below: int = 0
    # 1 + the number of distinct earlier prices below ``price``.
    rank: int = 0
    # Highest earlier price that counts as good today, or None if it never
    # dropped that far; ``good_times`` counts stretches at or below it.
    good_price: float | None = None
    good_times: int = 0


def _observations(entries: object) -> list[tuple[datetime.date, float]]:
    """Dated, numerically priced observations, one per date, oldest first."""
    if not isinstance(entries, (list, tuple)):
        return []
    by_date: dict[datetime.date, float] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        raw_date = entry.get("date")
        price = _as_float(entry.get("price"))
        if not isinstance(raw_date, str) or price is None:
            continue
        try:
            day = datetime.date.fromisoformat(raw_date)
        except ValueError:
            continue
        by_date[day] = round(price, 2)
    return sorted(by_date.items())


def _typical_price(
    observations: list[tuple[datetime.date, float]], observed_on: datetime.date
) -> float:
    """Median price over days rather than over checks.

    Each check counts for the days until the next check (capped), so a sale
    week watched daily weighs no more than the same week watched once. An exact
    half-and-half split resolves to the cheaper price, which makes fewer prices
    look like deals.
    """
    weighted: list[tuple[float, int]] = []
    for index, (day, price) in enumerate(observations):
        until = (
            observations[index + 1][0] if index + 1 < len(observations) else observed_on
        )
        weighted.append((price, min((until - day).days, MAX_HOLD_DAYS)))
    weighted.sort()
    half = sum(days for _, days in weighted) / 2
    running = 0
    for price, days in weighted:
        running += days
        if running >= half:
            break
    return price


def _stretches_at_or_below(
    observations: list[tuple[datetime.date, float]], ceiling: float
) -> int:
    """Count runs of consecutive checks at or below ceiling.

    A run only ends at an observed higher price, so a sale watched daily is
    one stretch, not one per check.
    """
    stretches = 0
    inside = False
    for _, price in observations:
        if price > ceiling:
            inside = False
        elif not inside:
            stretches += 1
            inside = True
    return stretches


def grade_price(
    entries: object, price: object, observed_on: datetime.date
) -> DealGrade:
    """Grade price, seen on observed_on, against the history entries before it.

    entries is what ``load_price_history`` returns. Never raises: malformed
    entries are skipped and a missing or non-finite price grades ``new``.
    """
    if isinstance(observed_on, datetime.datetime):
        observed_on = observed_on.date()
    earlier = [obs for obs in _observations(entries) if obs[0] < observed_on]
    checks = len(earlier)
    first_date = earlier[0][0] if earlier else None
    current = _as_float(price)
    if (
        current is None
        or first_date is None
        or checks < MIN_CHECKS
        or (observed_on - first_date).days < MIN_SPAN_DAYS
    ):
        return DealGrade(
            "new",
            None if current is None else round(current, 2),
            observed_on,
            checks,
            first_date,
        )

    current = round(current, 2)
    prices = [p for _, p in earlier]
    lowest = min(prices)
    lowest_date = max(day for day, p in earlier if p == lowest)
    typical = _typical_price(earlier, observed_on)
    below_usual = round(typical * (1 - MEANINGFUL_CHANGE), 2)
    above_usual = round(typical * (1 + MEANINGFUL_CHANGE), 2)
    good_ceiling = min(
        below_usual, round(lowest + (typical - lowest) * GOOD_RANGE_SHARE, 2)
    )

    grade: Grade
    if current < typical and current <= lowest and current <= below_usual:
        grade = "lowest"
    elif current < typical and current <= good_ceiling:
        grade = "good"
    elif current > typical and current >= above_usual:
        grade = "high"
    else:
        grade = "typical"

    good_price = max(
        (p for p in prices if p <= good_ceiling and p < typical), default=None
    )
    return DealGrade(
        grade=grade,
        price=current,
        observed_on=observed_on,
        checks=checks,
        first_date=first_date,
        lowest_price=lowest,
        lowest_date=lowest_date,
        typical_price=typical,
        times_at_or_below=_stretches_at_or_below(earlier, current),
        rank=1 + len({p for p in prices if p < current}),
        good_price=good_price,
        good_times=(
            0 if good_price is None else _stretches_at_or_below(earlier, good_price)
        ),
    )


def grade_latest(entries: object) -> DealGrade | None:
    """Grade the most recent recorded price against the entries before it."""
    observations = _observations(entries)
    if not observations:
        return None
    day, price = observations[-1]
    return grade_price(entries, price, day)


def grade_products(
    products: Iterable[Product],
    histories: Mapping[str, object],
    observed_on: datetime.date,
) -> dict[str, DealGrade]:
    """Grade each priced product by ASIN from preloaded marketplace histories."""
    return {
        product.asin: grade_price(
            histories.get(history_key(product.asin, product.locale), ()),
            product.price,
            observed_on,
        )
        for product in products
        if _as_float(product.price) is not None
    }
