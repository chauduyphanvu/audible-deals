"""Reusable recap, notification, and webhook delivery workflow."""

from __future__ import annotations

import json as json_mod
import logging
import sys

import click

from audible_deals.presentation.reports import display_recap
from audible_deals.presentation.terminal import console
from audible_deals.price_history import (
    find_all_atl_hits,
    find_wishlist_atl_hits,
    find_wishlist_hits,
    has_price_history,
    load_all_price_histories,
    scan_price_changes,
)
from audible_deals.webhook_client import WebhookClient, WebhookDeliveryError
from audible_deals.webhooks import (
    format_recap_payload,
    parse_webhook_headers as _parse_wh_headers,
)

logger = logging.getLogger(__name__)


def parse_webhook_headers(raw: tuple[str, ...]) -> dict[str, str]:
    """Parse 'Name: Value' strings into a headers dict. Raises UsageError on bad input."""
    try:
        return _parse_wh_headers(raw, strict=True)
    except ValueError as e:
        raise click.UsageError(str(e))


def empty_recap_payload(days: int, include_atl: bool) -> dict:
    payload: dict = {"days": days, "drops": [], "new_count": 0, "wishlist_hits": []}
    if include_atl:
        payload["atl_hits"] = []
    return payload


def _build_recap_payload(
    days: int,
    drops: list[tuple[str, str, float, float]],
    new_items: list,
    wishlist_hits: list[dict],
    atl_hits: list[dict] | None,
) -> dict:
    """Build the JSON/webhook recap payload from scanned price changes."""

    def _drop_pct(old: float, new: float) -> int:
        return round((old - new) / old * 100) if old > 0 else 0

    payload: dict = {
        "days": days,
        "drops": [
            {
                "asin": asin,
                "title": title,
                "old_price": old,
                "new_price": new,
                "drop_pct": _drop_pct(old, new),
            }
            for asin, title, old, new in sorted(
                drops, key=lambda x: x[2] - x[3], reverse=True
            )
        ],
        "new_count": len(new_items),
        "wishlist_hits": [
            {"asin": h["asin"], "title": h.get("title", "")} for h in wishlist_hits
        ],
    }
    if atl_hits is not None:
        payload["atl_hits"] = [
            {
                "asin": h["asin"],
                "title": h.get("title", ""),
                "price": h["price"],
                "target": h.get("target"),
            }
            for h in atl_hits
        ]
    return payload


def run_recap(
    days,
    show_new,
    atl,
    atl_all,
    json_flag,
    webhook,
    webhook_format,
    currency,
    extra_headers=None,
    locale="us",
):
    logger.info(
        "recap days=%s show_new=%s atl=%s atl_all=%s", days, show_new, atl, atl_all
    )
    if json_flag:
        console.file = sys.stderr
    histories = load_all_price_histories(locale)
    drops, new_items = scan_price_changes(days, histories=histories)
    if not drops and not new_items and not has_price_history(locale):
        if json_flag:
            empty = empty_recap_payload(days, atl or atl_all)
            click.echo(json_mod.dumps(empty, indent=2))
            return
        console.print(
            "[dim]No price history yet. Run 'deals find' or 'deals search' to start tracking.[/dim]"
        )
        return
    wishlist_hits = find_wishlist_hits(locale)
    if atl_all:
        atl_hits: list[dict] | None = find_all_atl_hits(
            histories=histories, locale=locale
        )
    elif atl:
        atl_hits = find_wishlist_atl_hits(locale)
    else:
        atl_hits = None

    if json_flag or webhook:
        payload = _build_recap_payload(days, drops, new_items, wishlist_hits, atl_hits)
        if json_flag:
            click.echo(json_mod.dumps(payload, indent=2))
            return
        if webhook:
            nothing = (
                not drops
                and not new_items
                and not wishlist_hits
                and not (atl_hits or [])
            )
            if nothing:
                console.print("[dim]Nothing to send.[/dim]")
                return
            try:
                body, headers = format_recap_payload(
                    payload, webhook_format, currency=currency
                )
            except ValueError as e:
                raise click.ClickException(str(e))
            if extra_headers:
                headers = {**headers, **extra_headers}
            try:
                WebhookClient().post(webhook, body, headers)
            except WebhookDeliveryError as exc:
                raise click.ClickException(str(exc)) from exc
            console.print("[green]Sent recap to webhook[/green]")
            return

    display_recap(
        drops,
        new_items,
        wishlist_hits,
        days,
        currency,
        show_new,
        atl_hits=atl_hits,
        atl_all=atl_all,
    )
