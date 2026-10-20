"""The ``--json`` payloads of the commands that report state rather than results.

Every payload here is an object with a top-level ``"schema_version": 1``.
(Older ``--json`` outputs -- ``find``, ``search``, ``last``, ``library``,
``history``, ``recap``, ``wishlist list``, ``notify``, ``series`` -- keep their
shapes, lists included, and count as schema version 1 too.) The shapes are
documented in ``docs/json.md`` and pinned by ``tests/golden/json``.

Builders are pure functions of already-loaded data, so they carry no terminal
or storage dependencies and are easy to test.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from audible_deals.metrics import buy_verdict, effective_price
from audible_deals.product import Product
from audible_deals.serialization import serialize_product

SCHEMA_VERSION = 1

#: Config keys whose values are secrets; ``config list/get --json`` redacts them.
_REDACTED = "<redacted>"


def dumps(payload: object) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)


def with_schema(**fields: Any) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, **fields}


# -- doctor ------------------------------------------------------------------


def doctor_payload(rows: Iterable[tuple[str, str, str]]) -> dict[str, Any]:
    checks = [
        {"name": name, "status": status.lower(), "detail": detail}
        for name, status, detail in rows
    ]
    failures = sum(1 for check in checks if check["status"] == "fail")
    warnings = sum(1 for check in checks if check["status"] == "warn")
    return with_schema(
        ok=failures == 0, failures=failures, warnings=warnings, checks=checks
    )


# -- watch -------------------------------------------------------------------


def watch_status(product: Product, target: float | None) -> str:
    if product.price is None:
        return "unavailable"
    if target is not None and product.price <= target:
        return "buy"
    if product.discount_pct and product.discount_pct > 0:
        return "discounted"
    return "waiting"


def watch_payload(
    products: Sequence[Product],
    targets: Mapping[str, float | None],
    *,
    buy_only: bool,
    missing: Sequence[str],
    author_watches: int,
    credit_price: float | None,
) -> dict[str, Any]:
    items = []
    hits = 0
    for product in products:
        target = targets.get(product.asin)
        status = watch_status(product, target)
        hits += status == "buy"
        if buy_only and status != "buy":
            continue
        items.append(
            {
                "asin": product.asin,
                "title": product.full_title,
                "authors": list(product.authors),
                "price": product.price,
                "list_price": product.list_price,
                "currency": product.currency,
                "target": target,
                "status": status,
                "verdict": (
                    buy_verdict(product, credit_price)
                    if credit_price is not None
                    else None
                ),
                "url": product.url,
            }
        )
    return with_schema(
        hits=hits,
        items=items,
        missing=list(missing),
        author_watches=author_watches,
    )


# -- track status ------------------------------------------------------------


def track_status_payload(
    *,
    install: Mapping[str, Any] | None,
    present: bool,
    where: str,
    enabled_monitors: Sequence[str],
    failed_monitors: Sequence[str],
    runs: Sequence[Mapping[str, Any]],
    include_history: bool,
) -> dict[str, Any]:
    payload = with_schema(
        installed=bool(install) or present,
        schedule=(
            {
                "every": install.get("every"),
                "method": install.get("method"),
                "installed_at": install.get("installed_at"),
                "present": present,
                "where": where,
            }
            if install
            else ({"present": True, "where": where} if present else None)
        ),
        monitors={
            "enabled": len(enabled_monitors),
            "with_errors": list(failed_monitors),
        },
        last_run=dict(runs[0]) if runs else None,
    )
    if include_history:
        payload["history"] = [dict(run) for run in runs]
    return payload


# -- monitors ----------------------------------------------------------------


def monitor_entry(
    name: str,
    definition: Mapping[str, Any],
    *,
    matches: int,
    last_success: str | None,
    last_error: str | None,
    estimated_calls: int | None,
    problem: str | None = None,
    include_settings: bool = False,
) -> dict[str, Any]:
    """One monitor, never including its webhook URL, headers or template."""
    settings = definition.get("settings")
    settings = settings if isinstance(settings, Mapping) else {}
    query = (
        definition.get("query")
        if definition.get("mode") == "search"
        else settings.get("keywords")
    )
    entry: dict[str, Any] = {
        "name": name,
        "valid": problem is None,
        "enabled": bool(definition.get("enabled", True)),
        "mode": definition.get("mode"),
        "locale": definition.get("locale"),
        "query": query or None,
        "profile": definition.get("profile") or None,
        "has_webhook": bool(definition.get("webhook")),
        "estimated_calls": estimated_calls,
        "matches": matches,
        "last_success": last_success,
        "last_error": last_error,
    }
    if problem is not None:
        entry["error"] = problem
    if include_settings:
        entry["settings"] = plain(dict(sorted(settings.items())))
    return entry


def plain(value: Any) -> Any:
    """*value* with tuples as lists and anything unusual as text, ready for JSON."""
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [plain(item) for item in value]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return (
            value
            if value == value and value not in (float("inf"), float("-inf"))
            else None
        )
    return str(value)


# -- config and profiles -----------------------------------------------------


#: The config keys whose values are secrets (a URL carries its token; headers carry theirs).
SECRET_CONFIG_KEYS = frozenset({"webhook", "webhook_headers"})


def redact_config_value(key: str, value: Any) -> Any:
    """What may be shown of the config setting *key*: never a secret.

    A webhook is ``<redacted>``; the headers keep their names (``Name: <redacted>``).
    The one place that decides this, for ``config`` text output and ``--json`` alike.
    Other keys are returned unchanged.
    """
    if key == "webhook":
        return _REDACTED
    if key == "webhook_headers":
        names = []
        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str):
                    name, separator, _ = item.partition(":")
                    if separator and name.strip():
                        names.append(f"{name.strip()}: {_REDACTED}")
        return names or _REDACTED
    return value


def redact_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """The config with webhook URLs and header values replaced."""
    return {key: redact_config_value(key, value) for key, value in config.items()}


# -- products ----------------------------------------------------------------


def product_payload(product: Product, credit_price: float | None) -> dict[str, Any]:
    return with_schema(
        product=serialize_product(product),
        credit_price=credit_price,
        verdict=(
            buy_verdict(product, credit_price) if credit_price is not None else None
        ),
        effective_price=(
            _rounded(effective_price(product, credit_price))
            if credit_price is not None
            else None
        ),
    )


def compare_payload(
    products: Sequence[Product], missing: Sequence[str], credit_price: float | None
) -> dict[str, Any]:
    return with_schema(
        products=[
            {
                **serialize_product(product),
                "verdict": (
                    buy_verdict(product, credit_price)
                    if credit_price is not None
                    else None
                ),
            }
            for product in products
        ],
        missing=list(missing),
        credit_price=credit_price,
    )


def categories_payload(
    categories: Sequence[Mapping[str, str]], *, parent: str, locale: str
) -> dict[str, Any]:
    return with_schema(
        locale=locale,
        parent=parent or None,
        categories=[
            {"id": category.get("id", ""), "name": category.get("name", "")}
            for category in categories
        ],
    )


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 2)
