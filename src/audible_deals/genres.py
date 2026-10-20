"""Fuzzy genre-name matching against Audible's category list."""

from __future__ import annotations

import difflib
import logging
from collections.abc import Sequence

from audible_deals import constants

logger = logging.getLogger(__name__)


def match_genre(query: str, categories: Sequence[dict[str, str]]) -> tuple[str, str]:
    """Fuzzy-match a genre name to a category ``(id, name)``.

    Tries alias expansion, exact match, substring, then difflib.
    Raises ValueError if there is no match or the name is ambiguous.
    """
    names = [c["name"] for c in categories]
    names_lower = [n.lower() for n in names]

    # Normalize and expand aliases
    q_raw = query.strip().lower()
    q = constants.GENRE_ALIASES.get(q_raw, q_raw)
    if q != q_raw:
        logger.debug("resolve_genre alias %r -> %r", q_raw, q)

    # Exact match
    if q in names_lower:
        idx = names_lower.index(q)
        logger.debug(
            "resolve_genre exact match: %r -> %s", query, categories[idx]["name"]
        )
        return categories[idx]["id"], categories[idx]["name"]

    # Substring match
    matches = [i for i, n in enumerate(names_lower) if q in n]
    if len(matches) == 1:
        idx = matches[0]
        logger.debug(
            "resolve_genre substring match: %r -> %s", query, categories[idx]["name"]
        )
        return categories[idx]["id"], categories[idx]["name"]
    if len(matches) > 1:
        options = ", ".join(names[i] for i in matches)
        raise ValueError(
            f'Ambiguous genre "{query}" matches: {options}\n'
            "Use a more specific name or --category ID."
        )

    # Fuzzy match via difflib
    close = difflib.get_close_matches(q, names_lower, n=1, cutoff=0.5)
    if close:
        idx = names_lower.index(close[0])
        logger.debug(
            "resolve_genre fuzzy match: %r -> %s", query, categories[idx]["name"]
        )
        return categories[idx]["id"], categories[idx]["name"]

    available = ", ".join(names)
    raise ValueError(f'No genre matching "{query}".\nAvailable: {available}')
