"""Wishlist service behavior."""

from __future__ import annotations

import contextlib
import copy
import json
import math
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import audible_deals.constants as constants
import audible_deals.constants as constants_mod
import audible_deals.wishlist as wishlist_mod
import audible_deals.wishlist_service as wishlist_service
from audible_deals.wishlist import (
    WishlistMutationError,
    load_wishlist,
    save_wishlist,
)
from audible_deals.wishlist_service import (
    WishlistSourceChangedError,
    add_author_watch,
    add_products,
    plan_owned_purge,
    plan_product_add,
    plan_repair,
    purge_confirmed_asins,
    remove_entries,
    repair_wishlist,
    sync_products,
    update_targets,
)
from tests.conftest import make_product


def _track_concurrent_lock_entries(monkeypatch):
    real_lock = wishlist_service.wishlist_lock
    entry_barrier = threading.Barrier(2)
    entry_count_lock = threading.Lock()
    entry_count = 0

    @contextlib.contextmanager
    def tracked_lock():
        nonlocal entry_count
        with entry_count_lock:
            entry_count += 1
        entry_barrier.wait(timeout=5)
        with real_lock():
            yield

    monkeypatch.setattr(wishlist_service, "wishlist_lock", tracked_lock)
    return lambda: entry_count


def test_product_add_plan_dedupes_pending_and_preserves_existing_occurrences(
    tmp_config,
):
    save_wishlist(
        [
            {"asin": "OLD1", "max_price": None},
            {"asin": "BAD1", "max_price": -1},
        ]
    )

    plan = plan_product_add(["OLD1", "NEW1", "NEW1", "OLD1", "NEW2"])

    assert plan.pending_asins == ("NEW1", "NEW2")
    assert plan.already_present == ("OLD1", "OLD1")
    assert plan.valid_total == 1
    assert [issue.index for issue in plan.issues] == [1]


def test_concurrent_distinct_product_additions_preserve_both(tmp_config, monkeypatch):
    lock_entries = _track_concurrent_lock_entries(monkeypatch)
    products = [make_product(asin="RACE1"), make_product(asin="RACE2")]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda product: add_products([product], None), products)
        )

    assert lock_entries() == 2
    assert sum(len(result.added_products) for result in results) == 2
    assert {item["asin"] for item in load_wishlist()} == {"RACE1", "RACE2"}


def test_concurrent_same_product_addition_reports_race_without_duplicate(
    tmp_config, monkeypatch
):
    lock_entries = _track_concurrent_lock_entries(monkeypatch)
    product = make_product(asin="SAME1")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: add_products([product], 4.5), range(2)))

    assert lock_entries() == 2
    assert sorted(len(result.added_products) for result in results) == [0, 1]
    assert sorted(len(result.raced_asins) for result in results) == [0, 1]
    assert [item["asin"] for item in load_wishlist()] == ["SAME1"]


def test_all_existing_additions_do_not_rewrite_wishlist(tmp_config):
    original = (
        b'[{"asin":"BOOK1","max_price":null},'
        b'{"type":"author","author":"Writer","max_price":4}]'
    )
    constants.WISHLIST_FILE.write_bytes(original)

    product_result = add_products([make_product(asin="BOOK1")], None)
    author_result = add_author_watch("writer", 2)

    assert product_result.raced_asins == ("BOOK1",)
    assert not author_result.added
    assert constants.WISHLIST_FILE.read_bytes() == original


@pytest.mark.parametrize(
    "operation",
    [
        lambda: remove_entries(["MISS1"]),
        lambda: update_targets(["MISS1"], 2),
        lambda: sync_products([], None),
    ],
)
def test_existing_zero_effect_mutations_keep_their_write_policy(tmp_config, operation):
    original = b'[{"asin":"BOOK1","max_price":null}]'
    constants.WISHLIST_FILE.write_bytes(original)

    operation()

    assert constants.WISHLIST_FILE.read_bytes() != original
    assert load_wishlist() == [{"asin": "BOOK1", "max_price": None}]


def test_ordinary_mutations_preserve_invalid_entries_and_order(tmp_config):
    invalid_object = {"asin": "BAD1", "max_price": -1, "note": "keep"}
    invalid_scalar = "also keep"
    original = [
        invalid_object,
        {"asin": "GOOD1", "title": "Good", "max_price": 5},
        invalid_scalar,
        {"type": "author", "author": "Writer", "max_price": 3},
    ]
    save_wishlist(original)

    result = update_targets(["GOOD1"], 2)

    assert len(result.issues) == 2
    assert load_wishlist() == [
        invalid_object,
        {"asin": "GOOD1", "title": "Good", "max_price": 2},
        invalid_scalar,
        original[3],
    ]


def test_target_update_materializes_lazy_asins_before_lock(tmp_config, monkeypatch):
    save_wishlist([{"asin": "BOOK1", "title": "Book", "max_price": 5}])
    lock_held = False
    original_lock = wishlist_service.wishlist_lock

    @contextlib.contextmanager
    def tracked_lock():
        nonlocal lock_held
        with original_lock():
            lock_held = True
            try:
                yield
            finally:
                lock_held = False

    def lazy_asins():
        assert not lock_held
        yield "BOOK1"
        assert not lock_held
        yield "MISS1"

    monkeypatch.setattr(wishlist_service, "wishlist_lock", tracked_lock)

    result = update_targets(lazy_asins(), 2)

    assert [event.asin for event in result.events] == ["BOOK1", "MISS1"]
    assert load_wishlist()[0]["max_price"] == 2


def test_titleless_target_update_and_case_insensitive_author_mutations(tmp_config):
    save_wishlist(
        [
            {"asin": "BOOK1", "max_price": 8},
            {"type": "author", "author": "Some Writer", "max_price": 4},
        ]
    )

    update = update_targets(["BOOK1", "MISS1"], None)
    duplicate = add_author_watch("some writer", 2)
    removed = remove_entries(author="SOME WRITER")

    assert update.changes[0].title == ""
    assert update.not_found_asins == ("MISS1",)
    assert not duplicate.added
    assert removed.removed == 1
    assert load_wishlist() == [{"asin": "BOOK1", "max_price": None}]


@pytest.mark.parametrize(
    ("update", "added", "updated", "skipped", "actions"),
    [
        (False, 1, 0, 3, ["added"]),
        (True, 1, 3, 0, ["updated", "updated", "added", "updated"]),
    ],
)
def test_sync_repeated_asins_preserves_order_and_counters(
    tmp_config, update, added, updated, skipped, actions
):
    save_wishlist([{"asin": "OLD1", "title": "Old", "max_price": 9}])
    products = [
        make_product(asin="OLD1", title="Old first"),
        make_product(asin="OLD1", title="Old second"),
        make_product(asin="NEW1", title="New first"),
        make_product(asin="NEW1", title="New second"),
    ]

    result = sync_products(products, 3, update=update)

    assert (result.added, result.updated, result.skipped) == (
        added,
        updated,
        skipped,
    )
    assert [change.action for change in result.changes] == actions
    assert [item["asin"] for item in load_wishlist()] == ["OLD1", "NEW1"]


def test_added_product_and_author_keep_exact_wire_shape(tmp_config):
    product = make_product(asin="WIRE1", title="Wire Book", locale="uk")

    add_products([product], 5, locale="ca")
    add_author_watch("Wire Writer", 7)

    product_entry, author_entry = load_wishlist()
    assert product_entry == {
        "asin": "WIRE1",
        "title": "Wire Book",
        "max_price": 5,
        "added": product_entry["added"],
        "locale": "ca",
    }
    assert author_entry == {
        "type": "author",
        "author": "Wire Writer",
        "max_price": 7,
        "added": author_entry["added"],
    }


def test_repair_preserves_order_and_creates_exact_owner_only_backup(tmp_config):
    valid_book = {"asin": "GOOD1", "metadata": {"keep": True}}
    valid_author = {"type": "author", "author": "Writer", "max_price": 0}
    original = json.dumps(
        [valid_book, "invalid", valid_author],
        separators=(",", ":"),
    ).encode()
    constants.WISHLIST_FILE.write_bytes(original)

    result = repair_wishlist(plan_repair())

    assert load_wishlist() == [valid_book, valid_author]
    assert result.removed == 1
    assert result.backup.read_bytes() == original
    assert stat.S_IMODE(result.backup.stat().st_mode) == 0o600


def test_repair_rejects_changed_source_with_typed_error(tmp_config):
    constants.WISHLIST_FILE.write_bytes(b'[{"asin":"GOOD1"},"invalid"]')
    plan = plan_repair()
    replacement = b'[{"asin":"GOOD1"},{"asin":"NEW1"},"invalid"]'
    constants.WISHLIST_FILE.write_bytes(replacement)

    with pytest.raises(WishlistSourceChangedError):
        repair_wishlist(plan)

    assert constants.WISHLIST_FILE.read_bytes() == replacement
    assert not (tmp_config / "wishlist.json.bak").exists()


def test_purge_preserves_entries_added_after_plan(tmp_config):
    save_wishlist(
        [
            {"asin": "OWN1", "title": "Owned", "max_price": None},
            {"asin": "KEEP1", "title": "Keep", "max_price": None},
        ]
    )
    plan = plan_owned_purge()
    add_products([make_product(asin="LATER1")], None)
    confirmed = plan.owned_items({"OWN1"})

    result = purge_confirmed_asins(item["asin"] for item in confirmed)

    assert result.removed == 1
    assert [item["asin"] for item in load_wishlist()] == ["KEEP1", "LATER1"]


@pytest.mark.parametrize("contents", [b"{", b'{"notes":"keep"}\n'])
@pytest.mark.parametrize(
    "operation",
    [
        lambda: plan_product_add(["BOOK1"]),
        lambda: add_products([make_product(asin="BOOK1")], None),
        lambda: remove_entries(["BOOK1"]),
        lambda: update_targets(["BOOK1"], 1),
        lambda: sync_products([make_product(asin="BOOK1")], None),
        plan_owned_purge,
        lambda: purge_confirmed_asins(["BOOK1"]),
    ],
)
def test_mutation_services_leave_malformed_roots_byte_identical(
    tmp_config, contents, operation
):
    constants.WISHLIST_FILE.write_bytes(contents)

    with pytest.raises(WishlistMutationError):
        operation()

    assert constants.WISHLIST_FILE.read_bytes() == contents


def test_bugfixwishlist_semantic_inspector_rejects_bad_targets_and_entries_without_mutation():
    raw = [
        {"asin": "GOOD1", "max_price": None},
        {"asin": "GOOD2", "max_price": 0},
        {"type": "author", "author": "Good Author", "max_price": 0},
        {"asin": "NEG1", "max_price": -1},
        {"asin": "NAN1", "max_price": math.nan},
        {"asin": "INF1", "max_price": math.inf},
        {"asin": "STR1", "max_price": "5"},
        {"asin": "BOOL1", "max_price": True},
        {"asin": "bad/path", "max_price": None},
        {"type": "author", "author": " ", "max_price": 5},
        {"type": "author", "author": "No Target", "max_price": None},
        "not an object",
    ]
    before = copy.deepcopy(raw)

    inspection = wishlist_mod.inspect_wishlist(raw)

    assert [item["asin"] for item in inspection.asin_items] == ["GOOD1", "GOOD2"]
    assert [item["author"] for item in inspection.author_items] == ["Good Author"]
    assert [issue.index for issue in inspection.issues] == list(range(3, 12))
    assert raw == before


def test_bugfixwishlist_semantic_inspector_accepts_large_nonnegative_integer_target():
    target = 10**400

    inspection = wishlist_mod.inspect_wishlist([{"asin": "HUGE1", "max_price": target}])

    assert inspection.asin_items == [{"asin": "HUGE1", "max_price": target}]
    assert inspection.issues == []


def test_bugfixwishlist_mutation_loader_refuses_unreadable_wishlist(
    tmp_config, monkeypatch
):
    constants_mod.WISHLIST_FILE.write_text("[]")
    original_read_text = Path.read_text

    def fail_read(path, *args, **kwargs):
        if path == constants_mod.WISHLIST_FILE:
            raise OSError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_read)

    with pytest.raises(
        wishlist_mod.WishlistMutationError, match="Cannot modify wishlist"
    ):
        wishlist_mod.load_wishlist_for_mutation()
