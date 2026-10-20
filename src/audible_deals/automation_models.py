"""Typed runtime and persistence models for automation workflows."""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


def _copy(value):
    return copy.deepcopy(value)


@dataclass(frozen=True)
class MonitorDefinition:
    name: str = ""
    enabled: bool = True
    locale: str = "us"
    mode: str = "find"
    query: str = ""
    profile: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    webhook: str | None = None
    webhook_format: str | None = None
    version: int = 1
    created_at: str | None = None
    unknown_fields: dict[str, Any] = field(default_factory=dict, repr=False)
    present_fields: frozenset[str] | None = field(default=None, repr=False)

    _KNOWN = frozenset(
        {
            "version",
            "name",
            "enabled",
            "locale",
            "mode",
            "query",
            "profile",
            "settings",
            "webhook",
            "webhook_format",
            "created_at",
        }
    )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MonitorDefinition:
        known = {key: _copy(value) for key, value in data.items() if key in cls._KNOWN}
        return cls(
            name=known.get("name", ""),
            enabled=bool(known.get("enabled", True)),
            locale=known.get("locale", "us"),
            mode=known.get("mode", "find"),
            query=known.get("query", ""),
            profile=known.get("profile"),
            settings=known.get("settings", {})
            if isinstance(known.get("settings", {}), dict)
            else {},
            webhook=known.get("webhook"),
            webhook_format=known.get("webhook_format"),
            version=known.get("version", 1),
            created_at=known.get("created_at"),
            unknown_fields={
                key: _copy(value)
                for key, value in data.items()
                if key not in cls._KNOWN
            },
            present_fields=frozenset(key for key in data if key in cls._KNOWN),
        )

    def to_dict(self) -> dict[str, Any]:
        values = {
            "version": self.version,
            "name": self.name,
            "enabled": self.enabled,
            "locale": self.locale,
            "mode": self.mode,
            "query": self.query,
            "profile": self.profile,
            "settings": _copy(self.settings),
            "webhook": self.webhook,
            "webhook_format": self.webhook_format,
            "created_at": self.created_at,
        }
        present = self._KNOWN if self.present_fields is None else self.present_fields
        result = _copy(self.unknown_fields)
        result.update({key: _copy(values[key]) for key in values if key in present})
        return result


@dataclass(frozen=True)
class MonitorSnapshot:
    """What a monitor last saw: the baseline its next run is compared against."""

    initialized: bool = False
    products: dict[str, dict] = field(default_factory=dict)
    last_success: str | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class MonitorEvent:
    event: Literal["new", "price_drop"]
    monitor: str
    asin: str
    title: str
    price: float
    target: float
    url: str
    previous_price: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MonitorEvent:
        return cls(
            event=data["event"],
            monitor=data["monitor"],
            asin=data["asin"],
            title=data.get("title", ""),
            price=data["price"],
            target=data.get("target", data["price"]),
            url=data.get("url", ""),
            previous_price=data.get("previous_price"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "monitor": self.monitor,
            "asin": self.asin,
            "title": self.title,
            "price": self.price,
            "target": self.target,
            "url": self.url,
            "previous_price": self.previous_price,
        }


@dataclass(frozen=True)
class NotificationHit:
    asin: str
    title: str
    price: float
    target: float
    url: str
    author: str | None = None
    verdict: str | None = None
    effective_price: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NotificationHit:
        return cls(
            asin=data["asin"],
            title=data.get("title", ""),
            price=data["price"],
            target=data["target"],
            url=data.get("url", ""),
            author=data.get("author"),
            verdict=data.get("verdict"),
            effective_price=data.get("effective_price"),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "asin": self.asin,
            "title": self.title,
            "price": self.price,
            "target": self.target,
            "url": self.url,
        }
        if self.author is not None:
            result["author"] = self.author
        if self.verdict is not None:
            result["verdict"] = self.verdict
            result["effective_price"] = self.effective_price
        return result


@dataclass(frozen=True)
class TrackRunResult:
    at: str
    duration_s: float
    wishlist_checked: int = 0
    author_watches_checked: int = 0
    extra_tracked_checked: int = 0
    hits: int = 0
    suppressed: int = 0
    webhook_sent: bool = False
    monitors_checked: int = 0
    monitors_scheduled: int = 0
    monitor_events: int = 0
    monitor_failures: tuple[str, ...] = ()
    error: str | None = None
    wishlist_issues: tuple[Any, ...] = field(default_factory=tuple, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """The record kept in the run history (everything except the issues)."""
        record = asdict(self)
        del record["wishlist_issues"]
        record["monitor_failures"] = list(self.monitor_failures)
        return record

    def summary(self) -> str:
        text = (
            f"Refreshed {self.wishlist_checked} wishlist + "
            f"{self.extra_tracked_checked} tracked item(s); {self.hits} at target"
        )
        if self.webhook_sent:
            text += " (webhook sent)"
        elif self.suppressed:
            text += f" ({self.suppressed} suppressed by cooldown)"
        if self.monitors_checked:
            text += (
                f"; {self.monitors_checked} monitor(s), {self.monitor_events} event(s)"
            )
        if self.monitor_failures:
            text += f" ({len(self.monitor_failures)} monitor failure(s))"
        return text


@dataclass(frozen=True)
class MonitorRunResult:
    events: tuple[MonitorEvent, ...]
    baseline: bool


@dataclass(frozen=True)
class MonitorSelection:
    monitors: tuple[MonitorDefinition, ...]
    cursor: int


@dataclass(frozen=True)
class NotificationRunResult:
    hits: tuple[NotificationHit, ...]
    had_hits: bool
    suppressed: int = 0
    empty_wishlist: bool = False
    wishlist_issues: tuple[Any, ...] = ()
    pending_notify_state: dict | None = field(default=None, repr=False)


@dataclass(frozen=True)
class TrackRunRequest:
    locale: str
    currency: str
    credit_price: float | None
    cooldown: int
    webhook: str | None
    webhook_format: str
    webhook_headers: dict[str, str]


@dataclass(frozen=True)
class NotificationRequest:
    locale: str
    currency: str
    credit_price: float | None
    webhook: str | None = None
    webhook_format: str = "generic"
    webhook_template: Path | None = None
    cooldown: int | None = None
    webhook_headers: dict[str, str] = field(default_factory=dict)
