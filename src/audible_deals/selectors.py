"""Resolve product selectors, cached positions, and Audible marketplace URLs."""

from __future__ import annotations

import dataclasses
import re
import urllib.parse

import click

from audible_deals import constants, results_cache
from audible_deals.result_models import ResultSession
from audible_deals.validation import validate_asin


@dataclasses.dataclass(frozen=True)
class ResolvedSelector:
    asin: str
    title: str = ""
    locale: str | None = None
    description: str = ""


def _expand_ref_string(ref: str | int, label: str = "--last") -> list[int]:
    """Expand a position or a string such as ``1-3,5`` into integers."""
    if isinstance(ref, int):
        return [ref]
    expanded: list[int] = []
    for part in str(ref).split(","):
        part = part.strip()
        if not part:
            raise click.ClickException(f"Invalid {label} value: empty part in {ref!r}.")
        if "-" in part:
            halves = part.split("-", 1)
            try:
                start, end = int(halves[0]), int(halves[1])
            except ValueError:
                raise click.ClickException(
                    f"Invalid {label} range {part!r}: must be two integers separated by '-'."
                )
            if start > end:
                raise click.ClickException(
                    f"Invalid {label} range {part!r}: start must not exceed end."
                )
            if end - start >= 1000:
                raise click.ClickException(
                    f"Invalid {label} range {part!r}: width must be under 1000."
                )
            expanded.extend(range(start, end + 1))
        else:
            try:
                expanded.append(int(part))
            except ValueError:
                raise click.ClickException(
                    f"Invalid {label} value {part!r}: must be an integer or range like '1-3'."
                )
    return expanded


_URL_ASIN_RE = re.compile(r"^B[A-Z0-9]{9}$", re.IGNORECASE)
_DOMAIN_LOCALE = {domain: locale for locale, domain in constants.LOCALE_DOMAIN.items()}


def parse_audible_url(value: str) -> tuple[str, str] | None:
    """Return (ASIN, locale) for a recognized Audible product URL."""
    try:
        parsed = urllib.parse.urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError as exc:
        if value.lower().startswith(("http://", "https://")):
            raise click.BadParameter(f"Invalid Audible URL {value!r}: {exc}")
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    locale = _DOMAIN_LOCALE.get(host)
    if locale is None and not host.startswith("www."):
        locale = _DOMAIN_LOCALE.get(f"www.{host}")
    if locale is None:
        return None
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    if not parts or parts[0].lower() not in {"pd", "dp"}:
        return None
    matches = [part.upper() for part in parts[1:] if _URL_ASIN_RE.fullmatch(part)]
    if not matches:
        raise click.BadParameter(f"Audible product URL has no valid ASIN: {value!r}")
    return matches[-1], locale


def _cached_selectors(
    ref: str,
    label: str,
    session: ResultSession,
    visible: list[dict],
) -> list[ResolvedSelector]:
    refs = _expand_ref_string(ref, label=label)
    resolved: list[ResolvedSelector] = []
    for position in refs:
        if position < 1 or position > len(visible):
            raise click.ClickException(
                f"{label} {position} is out of range (current view has {len(visible)} result(s))."
            )
        item = visible[position - 1]
        asin = item.get("asin")
        if not asin:
            raise click.ClickException(
                f"{label} {position} points to a cache entry with no ASIN (cache may be corrupt)."
            )
        title = str(item.get("title") or asin)
        locale = str(item.get("locale") or session.locale)
        resolved.append(
            ResolvedSelector(
                asin=asin,
                title=title,
                locale=locale,
                description=f"Result #{position} from '{session.title}': {title} ({asin})",
            )
        )
    return resolved


def resolve_selectors(
    selectors: tuple[str, ...] | list[str],
    *,
    last_refs: tuple[str | int, ...] = (),
    single: bool = False,
    explicit_locale: str | None = None,
) -> tuple[list[ResolvedSelector], str | None]:
    """Resolve ASINs, Audible URLs, ``@N`` lists/ranges, and legacy ``--last``."""
    resolved: list[ResolvedSelector] = []
    cached_view: tuple[ResultSession, list[dict]] | None = None

    def resolve_cached(ref: str, label: str) -> list[ResolvedSelector]:
        nonlocal cached_view
        if cached_view is None:
            session = results_cache.load_result_session()
            cached_view = session, session.visible_results
        session, visible = cached_view
        return _cached_selectors(ref, label, session, visible)

    for selector in selectors:
        if selector.startswith("@"):
            if selector == "@":
                raise click.ClickException(
                    "Invalid selector '@': provide a result number."
                )
            resolved.extend(resolve_cached(selector[1:], "selector"))
            continue
        parsed_url = parse_audible_url(selector)
        if parsed_url is not None:
            asin, locale = parsed_url
            resolved.append(
                ResolvedSelector(
                    asin=asin,
                    locale=locale,
                    description=f"{asin} from Audible {locale.upper()} URL",
                )
            )
            continue
        validate_asin(selector)
        resolved.append(ResolvedSelector(asin=selector.upper()))
    for last_ref in last_refs:
        resolved.extend(resolve_cached(str(last_ref), "--last"))

    if single and len(resolved) != 1:
        raise click.ClickException(
            f"Selector expanded to {len(resolved)} results; this command accepts a single position or product."
        )

    inferred = {item.locale for item in resolved if item.locale}
    unique: list[ResolvedSelector] = []
    seen: set[str] = set()
    for item in resolved:
        if item.asin not in seen:
            unique.append(item)
            seen.add(item.asin)

    if explicit_locale is not None:
        locale = explicit_locale
    elif len(inferred) > 1:
        raise click.ClickException(
            "Selected products span conflicting Audible marketplaces: "
            + ", ".join(sorted(inferred))
            + ". Pass --locale explicitly to choose one."
        )
    else:
        locale = next(iter(inferred), None)
    return unique, locale


def resolve_last_references(refs: tuple[str | int, ...]) -> list[tuple[str, str]]:
    """Resolve legacy ``--last`` positions."""
    resolved, _ = resolve_selectors((), last_refs=refs)
    return [(item.asin, item.description) for item in resolved]
