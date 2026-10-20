"""A "best so far" view that fills in while a catalog scan is still running."""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Callable, Iterable

from rich.console import RenderableType
from rich.text import Text

from audible_deals.presentation.common import price_str
from audible_deals.presentation.products import (
    ProductDisplayOptions,
    _build_product_table,
    calculate_product_layout,
)
from audible_deals.presentation.terminal import safe_text
from audible_deals.product import Product

logger = logging.getLogger(__name__)

#: The preview is re-ranked at most this often (seconds); pages that arrive in
#: between are folded into the next pass, and :meth:`LiveResults.finish` runs a
#: last one. Ranking filters and sorts every title seen so far, so doing it for
#: each of a fast scan's pages would cost more than the scan.
MIN_RANK_INTERVAL_S = 0.4


class LiveResults:
    """The top few results among the pages that have arrived so far.

    *rank* turns every product seen so far into the final ordering (the same
    filters and sort the finished table uses), so the preview shows what the
    result would be if the scan stopped now. A failure here must never fail the
    scan, so it only freezes the preview.

    The ranking is throttled (*min_interval* seconds between passes): a page that
    arrives sooner is remembered and counted in the next pass, and
    :meth:`finish` brings the preview up to date at the end.
    """

    def __init__(
        self,
        rank: Callable[[list[Product]], list[Product]],
        options: ProductDisplayOptions,
        *,
        rows: int,
        width: Callable[[], int],
        min_interval: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._rank = rank
        self._options = options
        self._rows = rows
        self._width = width
        self._min_interval = (
            MIN_RANK_INTERVAL_S if min_interval is None else min_interval
        )
        self._clock = clock
        self._seen: dict[str, Product] = {}
        self._top: list[Product] = []
        self._broken = False
        self._stale = False  # titles have arrived that the preview has not ranked
        self._last_rank: float | None = None

    def add(self, products: Iterable[Product]) -> None:
        """Take in a page of products; re-rank the preview unless that was just done."""
        added = False
        for product in products:
            if product.asin not in self._seen:
                self._seen[product.asin] = product
                added = True
        if not added or self._broken:
            return
        self._stale = True
        if (
            self._last_rank is None
            or self._clock() - self._last_rank >= self._min_interval
        ):
            self._refresh()

    def finish(self) -> bool:
        """Rank whatever the throttle held back; ``True`` if the preview changed."""
        if not self._stale or self._broken:
            return False
        self._refresh()
        return True

    def _refresh(self) -> None:
        self._stale = False
        try:
            self._top = self._rank(list(self._seen.values()))[: self._rows]
        except Exception:
            logger.debug("live preview disabled after an error", exc_info=True)
            self._broken = True
        # Measured from when the pass ended, so a slow ranking is followed by a
        # pause of its own length at least and never keeps the scan waiting.
        self._last_rank = self._clock()

    @property
    def seen(self) -> int:
        return len(self._seen)

    def view(self) -> RenderableType:
        """What to draw under the progress bar."""
        if not self._top:
            return Text(f"  Looking through {self.seen} titles...", style="dim")
        options = dataclasses.replace(
            self._options, title=f"Best {len(self._top)} so far"
        )
        layout = calculate_product_layout(self._top, options, self._width())
        if layout.mode == "cards":
            lines = Text()
            for index, product in enumerate(self._top, 1):
                lines.append(f"@{index} ", style="bold cyan")
                lines.append(safe_text(product.full_title))
                lines.append(
                    f"  {price_str(product.price, product.currency)}\n", style="green"
                )
            return lines
        return _build_product_table(self._top, options, layout)
