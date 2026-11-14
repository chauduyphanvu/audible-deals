"""Credit planner command."""

from __future__ import annotations

import json as json_mod
import logging
import sys

import click

from audible_deals.cli.helpers import _credit_price, _currency, _get_client
from audible_deals.credit_plan import CreditPlan, plan_credits
from audible_deals.presentation.credits import display_credit_plan
from audible_deals.presentation.terminal import console
from audible_deals.product import Product
from audible_deals.serialization import serialize_product
from audible_deals.wishlist import load_wishlist, partition_wishlist

logger = logging.getLogger(__name__)


def _empty_wishlist_error(audible_wishlist: bool) -> click.ClickException:
    hint = "Add books with 'deals wishlist add ASIN'"
    if not audible_wishlist:
        hint += ", or pass --audible-wishlist to use your Audible account wishlist"
    return click.ClickException(f"Your wishlist is empty. {hint}.")


def _plan_json(plan: CreditPlan) -> dict:
    return {
        "credits": plan.credits,
        "credit_price": plan.credit_price,
        "credit": [serialize_product(p) for p in plan.credit],
        "cash": [serialize_product(p) for p in plan.cash],
        "plus": [serialize_product(p) for p in plan.plus],
        "wait": [serialize_product(p) for p in plan.wait],
        "unavailable": plan.unavailable,
        "totals": {
            "cash_total": plan.cash_total,
            "credit_value": plan.credit_value,
            "value_per_credit": plan.value_per_credit,
            "credits_unused": plan.credits_unused,
            "saved": plan.saved,
        },
    }


@click.command("credits")
@click.argument("n", type=click.IntRange(min=1), default=1, metavar="[N]")
@click.option(
    "--audible-wishlist",
    is_flag=True,
    default=False,
    help="Also use your Audible account wishlist (on by default when the "
    "audible-wishlist setting is true)",
)
@click.option(
    "--json", "json_flag", is_flag=True, default=False, help="Output as JSON to stdout"
)
@click.pass_context
def credits_cmd(ctx, n, audible_wishlist, json_flag):
    """Plan which wishlist books to buy with N credits (default 1).

    Spends credits on the most expensive books, buys books cheaper than a
    credit with cash, and leaves the rest. Set what you pay per credit with
    'deals config set credit-price'. Books you own are left out.

    \b
    Examples:
        deals credits
        deals credits 3
        deals credits 2 --audible-wishlist
        deals credits 3 --json
    """
    logger.info("credits n=%s audible_wishlist=%s", n, audible_wishlist)
    locale = ctx.obj["locale"]
    credit_price = _credit_price(ctx)
    audible_wishlist = (
        audible_wishlist or ctx.obj.get("config", {}).get("audible_wishlist") is True
    )
    if json_flag:
        console.file = sys.stderr

    asin_items, _ = partition_wishlist(load_wishlist())
    active = [item for item in asin_items if item.get("locale", locale) == locale]
    local_asins = list(dict.fromkeys(item["asin"] for item in active))
    skipped = len(asin_items) - len(active)
    if skipped:
        noun = "entry" if skipped == 1 else "entries"
        console.print(
            f"[dim]Skipped {skipped} wishlist {noun} from other marketplaces.[/dim]"
        )
    if not local_asins and not audible_wishlist:
        raise _empty_wishlist_error(audible_wishlist)

    books: dict[str, Product] = {}
    dc = _get_client(locale)
    with dc:
        if audible_wishlist:
            books.update((p.asin, p) for p in dc.get_wishlist())
        missing = [asin for asin in local_asins if asin not in books]
        if missing:
            for product in dc.get_products_batch(missing):
                books.setdefault(product.asin, product)
        if not books and not local_asins:
            raise _empty_wishlist_error(audible_wishlist)
        owned = dc.get_library_asins()

    plan = plan_credits(
        [p for asin, p in books.items() if asin not in owned], n, credit_price
    )
    if json_flag:
        click.echo(
            json_mod.dumps(
                _plan_json(plan), indent=2, ensure_ascii=False, allow_nan=False
            )
        )
    else:
        display_credit_plan(plan, _currency(ctx))
