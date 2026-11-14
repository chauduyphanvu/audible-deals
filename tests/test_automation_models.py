"""Persistence contract tests for automation models."""

from audible_deals.automation_models import (
    MonitorDefinition,
    MonitorEvent,
    MonitorSnapshot,
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


def test_monitor_snapshot_roundtrip_preserves_sparse_and_unknown_fields():
    raw = {
        "products": {"A1": {"asin": "A1", "price": 3}},
        "future_health": "ok",
    }

    snapshot = MonitorSnapshot.from_dict(raw)

    assert snapshot.to_dict() == raw


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


def test_below_credit_hit_roundtrips_its_reason():
    hit = {
        "asin": "A1",
        "title": "Book",
        "price": 4.99,
        "target": 11.25,
        "url": "https://example.test/A1",
        "verdict": "cash",
        "effective_price": 4.99,
        "reason": "below_credit_price",
    }

    model = NotificationHit.from_dict(hit)

    assert model.reason == "below_credit_price"
    assert model.to_dict() == hit


def test_track_result_writes_audible_wishlist_fields_only_when_used():
    assert "audible_wishlist_error" not in TrackRunResult("now", 0).to_dict()

    used = TrackRunResult("now", 0, audible_wishlist_checked=3).to_dict()
    assert used["audible_wishlist_checked"] == 3
    assert used["audible_wishlist_error"] is None

    failed = TrackRunResult("now", 0, audible_wishlist_error="RuntimeError: x")
    assert TrackRunResult.from_dict(failed.to_dict()).to_dict() == failed.to_dict()
    assert "Audible wishlist failed: RuntimeError: x" in failed.summary()


def test_track_result_normalizes_failure_lists_and_preserves_sparse_wire_shape():
    success = {
        "at": "2026-08-21T12:00:00",
        "duration_s": 1.2,
        "wishlist_checked": 1,
        "monitor_failures": ["bad"],
        "error": None,
    }
    failure = {
        "at": "2026-08-21T12:00:00",
        "duration_s": 0.1,
        "error": "RuntimeError: boom",
    }

    success_model = TrackRunResult.from_dict(success)

    assert success_model.monitor_failures == ("bad",)
    assert success_model.to_dict() == success
    assert TrackRunResult.from_dict(failure).to_dict() == failure


def test_zero_known_fields_roundtrip_and_models_detach_nested_values():
    definition_source = {"future": {"nested": [1]}}
    definition = MonitorDefinition.from_dict(definition_source)
    definition_source["future"]["nested"].append(2)
    definition_wire = definition.to_dict()
    definition_wire["future"]["nested"].append(3)

    track_source = {"future": {"nested": [1]}}
    track = TrackRunResult.from_dict(track_source)
    track_source["future"]["nested"].append(2)
    track_wire = track.to_dict()
    track_wire["future"]["nested"].append(3)

    assert definition.to_dict() == {"future": {"nested": [1]}}
    assert track.to_dict() == {"future": {"nested": [1]}}
    assert MonitorDefinition().to_dict()["version"] == 1
    assert set(TrackRunResult(at="now", duration_s=0).to_dict()) == set(
        {
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
    )
