"""Persistence contract tests for automation models."""

from audible_deals.automation_models import (
    MonitorDefinition,
    MonitorEvent,
    NotificationHit,
    TrackRunResult,
)


def test_monitor_definition_roundtrip_preserves_sparse_and_unknown_fields():
    raw = {
        "name": "cheap",
        "settings": {"pages": 2, "exclude_authors": ["One"]},
        "future": {"nested": [1, 2]},
    }

    model = MonitorDefinition.from_dict(raw)

    assert model.to_dict() == raw
    assert model.unknown_fields == {"future": {"nested": [1, 2]}}


def test_event_and_notification_hit_roundtrip_exact_wire_shape():
    event = {
        "event": "price_drop",
        "monitor": "cheap",
        "asin": "A1",
        "title": "Book",
        "price": 2.99,
        "target": 2.99,
        "url": "https://example.test/A1",
        "previous_price": 4.0,
    }
    hit = {
        "asin": "A1",
        "title": "Book",
        "price": 2.99,
        "target": 3.0,
        "url": "https://example.test/A1",
        "author": "Writer",
        "verdict": "BUY",
        "effective_price": 2.99,
    }

    assert MonitorEvent.from_dict(event).to_dict() == event
    assert NotificationHit.from_dict(hit).to_dict() == hit


def test_track_result_record_lists_every_field_and_copies_failures():
    result = TrackRunResult(
        at="2026-08-21T12:00:00",
        duration_s=1.2,
        wishlist_checked=1,
        monitor_failures=("bad",),
        wishlist_issues=("not persisted",),
    )

    record = result.to_dict()

    assert record["monitor_failures"] == ["bad"]
    assert isinstance(record["monitor_failures"], list)
    assert "wishlist_issues" not in record
    assert set(record) == {
        "at",
        "duration_s",
        "wishlist_checked",
        "author_watches_checked",
        "extra_tracked_checked",
        "hits",
        "suppressed",
        "webhook_sent",
        "monitors_checked",
        "monitors_scheduled",
        "monitor_events",
        "monitor_failures",
        "error",
    }


def test_zero_known_fields_roundtrip_and_definitions_detach_nested_values():
    definition_source = {"future": {"nested": [1]}}
    definition = MonitorDefinition.from_dict(definition_source)
    definition_source["future"]["nested"].append(2)
    definition_wire = definition.to_dict()
    definition_wire["future"]["nested"].append(3)

    assert definition.to_dict() == {"future": {"nested": [1]}}
    assert MonitorDefinition().to_dict()["version"] == 1
