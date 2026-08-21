"""Persistent result sessions, selectors, and cumulative seen-ASIN storage."""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import math
import re
import urllib.parse
from typing import Any

import click

from audible_deals import constants
from audible_deals.storage import _atomic_write, load_json_file

logger = logging.getLogger(__name__)

SESSION_VERSION = 2

_RECIPE_LIST_FIELDS = {
    "exclude_authors",
    "exclude_narrators",
    "exclude_keywords",
    "exclude_genres",
}
_RECIPE_BOOL_FIELDS = {
    "on_sale",
    "first_in_series",
    "skip_plus",
    "only_plus",
    "require_history",
    "skip_owned",
    "exclude_seen",
}
_RECIPE_TEXT_FIELDS = {
    "narrator",
    "author",
    "series",
    "publisher",
    "language",
    "sort",
    "released_after",
    "released_before",
}
_RECIPE_NUMBER_FIELDS = {
    "max_price",
    "max_pph",
    "max_effective_price",
    "min_rating",
    "min_ratings",
    "min_hours",
    "min_discount",
    "limit",
    "hist_below",
    "min_price_drop",
}


@dataclasses.dataclass
class ResultSession:
    """One persistent, locally refinable result set."""

    producer: str
    locale: str
    title: str
    source: dict[str, Any]
    candidates: list[dict]
    baseline_recipe: dict[str, Any]
    current_recipe: dict[str, Any]
    visible_asins: list[str]
    constraints: dict[str, Any] = dataclasses.field(default_factory=dict)
    ranking_context: dict[str, Any] = dataclasses.field(default_factory=dict)
    timestamp: str = dataclasses.field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    version: int = SESSION_VERSION
    legacy: bool = False

    @property
    def visible_results(self) -> list[dict]:
        by_asin = {
            item.get("asin"): item
            for item in self.candidates
            if isinstance(item, dict) and item.get("asin")
        }
        return [by_asin[asin] for asin in self.visible_asins if asin in by_asin]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "producer": self.producer,
            "locale": self.locale,
            "timestamp": self.timestamp,
            "title": self.title,
            "source": self.source,
            "candidates": self.candidates,
            "baseline_recipe": self.baseline_recipe,
            "current_recipe": self.current_recipe,
            "visible_asins": self.visible_asins,
            "constraints": self.constraints,
            "ranking_context": self.ranking_context,
            "legacy": self.legacy,
            # Retained for readers written against the old object cache.
            "results": self.visible_results,
        }


@dataclasses.dataclass(frozen=True)
class ResolvedSelector:
    asin: str
    title: str = ""
    locale: str | None = None
    description: str = ""


def load_seen_asins() -> set[str]:
    """Load cumulative seen ASINs for exclusion."""
    return set(load_json_file(constants.SEEN_ASINS_FILE, list, "seen ASINs"))


def save_seen_asins(new_asins: set[str]) -> None:
    """Append ASINs to the cumulative seen-ASINs file."""
    if not new_asins:
        return
    existing = load_seen_asins()
    if new_asins <= existing:
        logger.debug("save_seen_asins: no new asins (%d already seen)", len(existing))
        return
    merged = sorted(existing | new_asins)
    try:
        _atomic_write(constants.SEEN_ASINS_FILE, json.dumps(merged))
        logger.debug(
            "saved seen ASINs (%d total, +%d new)",
            len(merged),
            len(merged) - len(existing),
        )
    except Exception:
        logger.warning(
            "failed to write seen-asins at %s", constants.SEEN_ASINS_FILE, exc_info=True
        )


def merge_seen_asins(
    skip_asins: set[str] | None, exclude_seen: bool
) -> set[str] | None:
    """Merge previously-seen ASINs into the skip set when requested."""
    if not exclude_seen:
        return skip_asins
    seen = load_seen_asins()
    if skip_asins is None:
        return seen
    return skip_asins | seen


def clear_seen_asins() -> bool:
    """Delete the cumulative seen-ASINs file. Returns True if deleted."""
    try:
        constants.SEEN_ASINS_FILE.unlink()
        logger.debug("cleared seen-asins: %s", constants.SEEN_ASINS_FILE)
        return True
    except FileNotFoundError:
        return False


def _read_cache_data() -> Any:
    if not constants.LAST_RESULTS_FILE.exists():
        raise click.ClickException(
            "No cached results found. Run 'deals find', 'deals search', "
            "'deals for-me', or 'deals series' first."
        )
    try:
        return json.loads(constants.LAST_RESULTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise click.ClickException(f"Could not read last results cache: {exc}")


def _require(value: Any, expected: type, field: str) -> Any:
    if not isinstance(value, expected):
        raise click.ClickException(
            f"Last results cache is corrupt: {field} must be {expected.__name__}."
        )
    return value


def _validate_recipe(recipe: dict[str, Any], field: str) -> None:
    for key in _RECIPE_LIST_FIELDS:
        value = recipe.get(key, [])
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise click.ClickException(
                f"Last results cache is corrupt: {field}.{key} must be a string list."
            )
    for key in _RECIPE_BOOL_FIELDS:
        if key in recipe and not isinstance(recipe[key], bool):
            raise click.ClickException(
                f"Last results cache is corrupt: {field}.{key} must be boolean."
            )
    for key in _RECIPE_TEXT_FIELDS:
        if key in recipe and not isinstance(recipe[key], str):
            raise click.ClickException(
                f"Last results cache is corrupt: {field}.{key} must be text."
            )
    for key in _RECIPE_NUMBER_FIELDS:
        value = recipe.get(key)
        if value is not None and (
            not isinstance(value, (int, float)) or isinstance(value, bool)
        ):
            raise click.ClickException(
                f"Last results cache is corrupt: {field}.{key} must be numeric."
            )


def _legacy_session(data: list[dict], title: str = "Last results") -> ResultSession:
    if not all(isinstance(item, dict) for item in data):
        raise click.ClickException(
            "Last results cache is corrupt: results must be objects."
        )
    if any(
        not isinstance(item.get("asin"), str) or not item.get("asin") for item in data
    ):
        raise click.ClickException("Last results cache contains an entry with no ASIN.")
    asins = [item.get("asin") for item in data if isinstance(item.get("asin"), str)]
    locale = next(
        (
            item["locale"]
            for item in data
            if item.get("locale") in constants.LOCALE_DOMAIN
        ),
        "us",
    )
    return ResultSession(
        producer="legacy",
        locale=locale,
        title=title,
        source={"command": "Run a new discovery command for true widening."},
        candidates=data,
        baseline_recipe={"sort": "", "limit": 0},
        current_recipe={"sort": "", "limit": 0},
        visible_asins=asins,
        legacy=True,
    )


def load_result_session() -> ResultSession:
    """Load and validate a result session, normalizing both legacy cache shapes."""
    data = _read_cache_data()
    if isinstance(data, list):
        return _legacy_session(data)
    if not isinstance(data, dict):
        raise click.ClickException("Last results cache is corrupt.")
    if data.get("version") != SESSION_VERSION:
        results = data.get("results")
        if isinstance(results, list):
            return _legacy_session(results, str(data.get("title") or "Last results"))
        raise click.ClickException("Last results cache is corrupt.")

    producer = _require(data.get("producer"), str, "producer")
    locale = _require(data.get("locale"), str, "locale")
    title = _require(data.get("title"), str, "title")
    source = _require(data.get("source"), dict, "source")
    candidates = _require(data.get("candidates"), list, "candidates")
    baseline = _require(data.get("baseline_recipe"), dict, "baseline_recipe")
    current = _require(data.get("current_recipe"), dict, "current_recipe")
    visible = _require(data.get("visible_asins"), list, "visible_asins")
    constraints = _require(data.get("constraints", {}), dict, "constraints")
    ranking = _require(data.get("ranking_context", {}), dict, "ranking_context")
    legacy = _require(data.get("legacy", False), bool, "legacy")
    timestamp = _require(data.get("timestamp", ""), str, "timestamp")
    if locale not in constants.LOCALE_DOMAIN:
        raise click.ClickException("Last results cache is corrupt: invalid locale.")
    if not isinstance(source.get("command"), str):
        raise click.ClickException(
            "Last results cache is corrupt: source.command must be text."
        )
    try:
        datetime.datetime.fromisoformat(timestamp)
    except ValueError:
        raise click.ClickException("Last results cache is corrupt: invalid timestamp.")
    _validate_recipe(baseline, "baseline_recipe")
    _validate_recipe(current, "current_recipe")
    if not all(isinstance(item, dict) for item in candidates):
        raise click.ClickException(
            "Last results cache is corrupt: candidates must be objects."
        )
    if any(
        not isinstance(item.get("asin"), str)
        or not item.get("asin")
        or not isinstance(item.get("title"), str)
        for item in candidates
    ):
        raise click.ClickException(
            "Last results cache is corrupt: every candidate needs an ASIN and title."
        )
    if any(
        "locale" in item
        and (
            not isinstance(item["locale"], str)
            or item["locale"] not in constants.LOCALE_DOMAIN
        )
        for item in candidates
    ):
        raise click.ClickException(
            "Last results cache is corrupt: candidate locale is invalid."
        )
    if not all(isinstance(asin, str) for asin in visible):
        raise click.ClickException(
            "Last results cache is corrupt: visible_asins must be strings."
        )
    candidate_asins = {
        item.get("asin") for item in candidates if isinstance(item.get("asin"), str)
    }
    if any(asin not in candidate_asins for asin in visible):
        raise click.ClickException(
            "Last results cache is corrupt: visible result is not in candidates."
        )
    credit_price = constraints.get("credit_price")
    if credit_price is not None and (
        not isinstance(credit_price, (int, float))
        or isinstance(credit_price, bool)
        or not math.isfinite(credit_price)
        or credit_price < 0
    ):
        raise click.ClickException(
            "Last results cache is corrupt: constraints.credit_price must be non-negative."
        )
    for field, value_type in (
        ("history_percentiles", int),
        ("price_drop_pcts", (int, float)),
    ):
        values = constraints.get(field)
        if values is not None and (
            not isinstance(values, dict)
            or any(
                not isinstance(key, str)
                or not isinstance(value, value_type)
                or isinstance(value, bool)
                or not math.isfinite(value)
                for key, value in values.items()
            )
        ):
            raise click.ClickException(
                f"Last results cache is corrupt: constraints.{field} is invalid."
            )
    return ResultSession(
        producer=producer,
        locale=locale,
        title=title,
        source=source,
        candidates=candidates,
        baseline_recipe=baseline,
        current_recipe=current,
        visible_asins=visible,
        constraints=constraints,
        ranking_context=ranking,
        timestamp=timestamp,
        legacy=legacy,
    )


def save_result_session(session: ResultSession) -> None:
    """Atomically persist a complete result session."""
    _atomic_write(
        constants.LAST_RESULTS_FILE,
        json.dumps(session.to_dict(), ensure_ascii=False),
    )
    logger.debug(
        "saved result session producer=%s candidates=%d visible=%d",
        session.producer,
        len(session.candidates),
        len(session.visible_asins),
    )


def load_last_results() -> tuple[str, list[dict]]:
    """Compatibility reader returning the current, limit-applied session view."""
    session = load_result_session()
    return session.title, session.visible_results


def save_last_results(title: str, serialized: list[dict]) -> None:
    """Compatibility writer for callers that only have a limited legacy result list."""
    cache_obj = {"title": title, "results": serialized}
    _atomic_write(
        constants.LAST_RESULTS_FILE, json.dumps(cache_obj, ensure_ascii=False)
    )


def update_session_view(ordered_asins: list[str], *, sort: str | None = None) -> None:
    """Persist the visible selector order and an optional interactive sort key."""
    session = load_result_session()
    session.visible_asins = list(ordered_asins)
    if sort is not None:
        session.current_recipe["sort"] = sort
    save_result_session(session)


def clear_last_results() -> bool:
    """Delete the last-results cache. Returns True if deleted."""
    try:
        constants.LAST_RESULTS_FILE.unlink()
        logger.debug("cleared last results cache: %s", constants.LAST_RESULTS_FILE)
        return True
    except FileNotFoundError:
        return False


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


def _cached_selectors(ref: str, label: str) -> list[ResolvedSelector]:
    session = load_result_session()
    refs = _expand_ref_string(ref, label=label)
    visible = session.visible_results
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
    for selector in selectors:
        if selector.startswith("@"):
            if selector == "@":
                raise click.ClickException(
                    "Invalid selector '@': provide a result number."
                )
            resolved.extend(_cached_selectors(selector[1:], "selector"))
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
        from audible_deals.validation import validate_asin

        validate_asin(selector)
        resolved.append(ResolvedSelector(asin=selector.upper()))
    for last_ref in last_refs:
        resolved.extend(_cached_selectors(str(last_ref), "--last"))

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
    """Compatibility wrapper resolving legacy ``--last`` positions."""
    resolved, _ = resolve_selectors((), last_refs=refs)
    return [(item.asin, item.description) for item in resolved]
