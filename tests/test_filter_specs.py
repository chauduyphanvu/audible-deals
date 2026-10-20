"""The filter registry and everything derived from it.

These tests fail when a hand-maintained artifact (Settings, ResultRecipe,
RecipePatch, FilterContext, the config schema, the click commands, the docs)
stops matching ``audible_deals.filter_specs.FILTERS``.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from audible_deals import constants
from audible_deals.cli import cli
from audible_deals.config_store import (
    CONFIG_SCHEMA,
    coerce_config_value,
    config_numeric_errors,
    load_config,
    validate_config_key,
)
from audible_deals.filter_specs import (
    FILTERS,
    INHERIT,
    SPEC_BY_KEY,
    Kind,
    Refine,
    Scope,
    active_filter_labels,
    config_bounds,
    config_key_aliases,
    recipe_specs,
    specs_for,
)
from audible_deals.result_models import (
    FilterContext,
    RecipePatch,
    ResultRecipe,
)
from audible_deals.settings import (
    Settings,
    filter_combination_error,
    profile_validation_error,
)

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Registry sanity
# ---------------------------------------------------------------------------


def test_registry_keys_flags_and_labels_are_unique():
    keys = [spec.key for spec in FILTERS]
    flags = [spec.flag for spec in FILTERS]
    labels = [spec.label for spec in FILTERS if spec.label]
    assert len(set(keys)) == len(keys)
    assert len(set(flags)) == len(flags)
    assert len(set(labels)) == len(labels)
    for spec in FILTERS:
        assert spec.flag.startswith("--"), spec.key
        assert spec.scopes, f"{spec.key} is available nowhere"
        assert Scope.CONFIG not in spec.variants


def test_every_variant_targets_a_scope_the_filter_is_in():
    for spec in FILTERS:
        assert set(spec.variants) <= spec.scopes, spec.key


def test_refinement_classes_match_scopes():
    for spec in FILTERS:
        if spec.refine is Refine.NONE:
            assert Scope.LAST not in spec.scopes, spec.key
        else:
            assert Scope.LAST in spec.scopes, spec.key


def test_numeric_defaults_respect_their_own_bounds():
    for spec in FILTERS:
        if spec.is_numeric and spec.default is not None:
            lower, upper = spec.lower_bound, spec.upper_bound
            assert lower is None or spec.default >= lower, spec.key
            assert upper is None or spec.default <= upper, spec.key
        for scope in spec.variants:
            value = spec.variants[scope].default
            if spec.is_numeric and value is not INHERIT and value is not None:
                assert spec.lower_bound is None or value >= spec.lower_bound


# ---------------------------------------------------------------------------
# Typed mirrors: Settings, ResultRecipe, RecipePatch, FilterContext
# ---------------------------------------------------------------------------


def test_settings_fields_and_defaults_match_the_registry():
    fields = {f.name for f in dataclasses.fields(Settings)}
    assert fields == {spec.key for spec in FILTERS}
    default = Settings()
    for spec in FILTERS:
        assert getattr(default, spec.key) == spec.default, spec.key


def test_recipe_and_patch_fields_match_the_registry():
    expected = {spec.recipe_name for spec in recipe_specs()}
    recipe_fields = {
        f.name for f in dataclasses.fields(ResultRecipe) if not f.name.startswith("_")
    }
    patch_fields = {f.name for f in dataclasses.fields(RecipePatch)}
    assert recipe_fields == expected
    assert patch_fields == expected


def test_recipe_field_defaults_agree_with_the_filter_kind():
    recipe = ResultRecipe()
    for spec in recipe_specs():
        value = getattr(recipe, spec.recipe_name)
        if spec.is_multi:
            assert value == (), spec.key
        elif spec.is_bool:
            assert value is False, spec.key
        elif spec.kind in (Kind.TEXT, Kind.DATE):
            assert value == "", spec.key
        else:
            assert value is None or isinstance(value, (int, float)), spec.key


# FilterContext fields that carry state rather than a user-facing filter.
_INTERNAL_CONTEXT_FIELDS = {
    "credit_price",
    "genre",
    "skip_asins",
    "exclude_category_ids",
    "drop_zero_length",
    "hist_percentile",
    "price_drops",
}


def test_filter_context_fields_are_covered_by_the_registry():
    context_fields = {f.name for f in dataclasses.fields(FilterContext)}
    declared = {spec.context_field for spec in FILTERS if spec.context_field}
    assert declared <= context_fields
    assert context_fields - declared == _INTERNAL_CONTEXT_FIELDS


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


def test_config_schema_is_registry_filters_plus_named_extras():
    config_filters = {spec.key: spec.config_type for spec in specs_for(Scope.CONFIG)}
    extras = {
        "locale": str,
        "credit_price": float,
        "webhook": str,
        "webhook_format": str,
        "webhook_headers": list,
        "webhook_allow_private": bool,
        "secret_storage": str,
        "cache_ttl": int,
        "known_candidates": bool,
    }
    assert CONFIG_SCHEMA == {**config_filters, **extras}


def test_config_keys_published_before_the_registry_still_exist():
    legacy = {
        "skip_owned": bool,
        "max_price": float,
        "max_pph": float,
        "min_rating": float,
        "min_ratings": int,
        "min_hours": float,
        "min_discount": int,
        "language": str,
        "locale": str,
        "sort": str,
        "pages": int,
        "on_sale": bool,
        "deep": bool,
        "first_in_series": bool,
        "all_languages": bool,
        "interactive": bool,
        "limit": int,
        "narrator": str,
        "author": str,
        "series": str,
        "publisher": str,
        "skip_plus": bool,
        "only_plus": bool,
        "credit_price": float,
        "webhook": str,
        "webhook_format": str,
        "webhook_headers": list,
    }
    # New filters may add keys; none of the published ones may disappear or
    # change type.
    for key, typ in legacy.items():
        assert CONFIG_SCHEMA[key] is typ, key


def test_config_bounds_match_the_flag_bounds():
    bounds = config_bounds()
    published = {
        "pages": (1, None),
        "limit": (0, None),
        "max_price": (0, None),
        "max_pph": (0, None),
        "max_effective_price": (0, None),
        "min_rating": (0, 5),
        "min_ratings": (0, None),
        "min_hours": (0, None),
        "min_discount": (0, 100),
    }
    for key, expected in published.items():
        assert bounds[key] == expected, key
    # every numeric config key is bounded by its registry spec
    assert set(bounds) == {s.key for s in specs_for(Scope.CONFIG) if s.is_numeric}


def test_max_price_per_hour_remains_a_config_key_alias():
    assert config_key_aliases() == {"max_price_per_hour": "max_pph"}
    assert validate_config_key("max-price-per-hour") == "max_pph"
    assert validate_config_key("max-effective-price") == "max_effective_price"


_SAMPLE_CONFIG_VALUES = {
    bool: "true",
    int: "3",
    float: "2.5",
    str: "english",
}


@pytest.mark.parametrize("spec", specs_for(Scope.CONFIG), ids=lambda s: s.key)
def test_every_config_filter_can_be_set_and_read_back(spec, tmp_config):
    if spec.valid is not None:
        raw = next(v for v in sorted(spec.valid) if not v.startswith("-"))
    else:
        raw = _SAMPLE_CONFIG_VALUES[spec.config_type]
    key = spec.flag_name
    runner = CliRunner()

    result = runner.invoke(cli, ["config", "set", key, raw])

    assert result.exit_code == 0, result.output
    stored = load_config()[spec.key]
    assert isinstance(stored, spec.config_type)
    got = runner.invoke(cli, ["config", "get", key])
    assert got.exit_code == 0 and spec.key in got.output
    assert config_numeric_errors(load_config()) == []


def test_config_set_help_lists_every_valid_key():
    help_text = CliRunner().invoke(cli, ["config", "set", "--help"]).output
    flat = " ".join(help_text.split())
    for key in CONFIG_SCHEMA:
        assert key.replace("_", "-") in flat, key
    assert "max-pph (or max-price-per-hour)" in flat


@pytest.mark.parametrize(
    ("key", "raw"),
    [
        ("max-effective-price", "-1"),
        ("max-effective-price", "nan"),
        ("min-discount", "101"),
        ("pages", "0"),
        ("sort", "bogus"),
    ],
)
def test_config_bounds_are_enforced(key, raw, tmp_config):
    result = CliRunner().invoke(cli, ["config", "set", "--", key, raw])

    assert result.exit_code == 1
    assert load_config() == {}


def test_coerce_config_value_rejects_unlisted_sorts():
    with pytest.raises(click.ClickException, match="Invalid sort value 'nope'"):
        coerce_config_value("sort", "nope")


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "message"),
    [
        ({"released_after": 5}, "released_after must be text"),
        ({"released_after": "soon"}, "released_after"),
        ({"released_before": "2024-1-1"}, "released_before"),
        ({"require_history": "yes"}, "require_history must be boolean"),
        ({"exclude_seen": 1}, "exclude_seen must be boolean"),
        ({"hist_below": 101}, "hist_below"),
        ({"hist_below": 2.5}, "hist_below"),
        ({"min_price_drop": -1}, "min_price_drop"),
        ({"max_effective_price": "cheap"}, "max_effective_price"),
        ({"sort": "bogus"}, "sort must be one of"),
        ({"genre": 3}, "genre must be text"),
        ({"deep": "yes"}, "deep must be boolean"),
        ({"exclude_keyword": "x"}, None),  # unknown keys are not an error here
        ({"exclude_keywords": "x"}, "exclude_keywords must be a list of text values"),
    ],
)
def test_profile_validation_uses_registry_kinds(profile, message):
    error = profile_validation_error(profile)
    if message is None:
        assert error is None
    else:
        assert error is not None and message in error


def test_profile_validation_reports_text_before_booleans_before_choices():
    error = profile_validation_error({"sort": "bogus", "deep": "no", "genre": 1})
    assert error == "genre must be text"
    error = profile_validation_error({"sort": "bogus", "deep": "no"})
    assert error == "deep must be boolean"


# ---------------------------------------------------------------------------
# Settings validation of the new fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_effective_price": -0.01},
        {"hist_below": 101},
        {"hist_below": -1},
        {"min_price_drop": float("inf")},
        {"released_after": "01/02/2024"},
        {"released_before": "20240102"},
    ],
)
def test_settings_reject_invalid_new_fields(overrides):
    with pytest.raises(ValueError, match="Invalid stored setting"):
        Settings(**overrides)


def test_settings_accept_valid_new_fields():
    settings = Settings(
        max_effective_price=5,
        hist_below=25,
        min_price_drop=30.5,
        require_history=True,
        released_after="2024-01-01",
        released_before="2024-12-31",
        exclude_seen=True,
    )
    assert settings.hist_below == 25


def test_filter_combination_rules():
    assert filter_combination_error(Settings()) is None
    assert "requires --hist-below" in (
        filter_combination_error(Settings(require_history=True)) or ""
    )
    assert (
        filter_combination_error(Settings(require_history=True, hist_below=10)) is None
    )
    assert (
        filter_combination_error(Settings(require_history=True, min_price_drop=5))
        is None
    )
    inverted = Settings(released_after="2025-01-02", released_before="2025-01-01")
    assert "cannot be later" in (filter_combination_error(inverted) or "")
    equal = Settings(released_after="2025-01-01", released_before="2025-01-01")
    assert filter_combination_error(equal) is None


def test_active_filter_labels_follow_registry_order():
    settings = Settings(
        max_price=5.0,
        min_ratings=1,
        author="Weir",
        on_sale=True,
        exclude_authors=("a", "b"),
        hist_below=0,
        released_before="2025-01-01",
    )
    assert active_filter_labels(settings) == [
        "max-price=5.0",
        "min-ratings=1",
        "author=Weir",
        "on-sale=yes",
        "exclude-authors=a,b",
        "hist-below=0",
        "released-before=2025-01-01",
    ]
    assert active_filter_labels(Settings()) == []


# ---------------------------------------------------------------------------
# The registry's JSON persistence stays readable by older/newer versions
# ---------------------------------------------------------------------------


def test_settings_round_trip_through_json_for_monitors():
    settings = Settings(hist_below=10, released_after="2024-01-01", max_pph=0.5)
    stored = json.loads(json.dumps(dataclasses.asdict(settings)))

    assert profile_validation_error(stored) is None
    restored = Settings(**stored)
    assert (restored.hist_below, restored.released_after, restored.max_pph) == (
        10,
        "2024-01-01",
        0.5,
    )


def test_constants_no_longer_hold_a_second_config_schema():
    assert not hasattr(constants, "_CONFIG_SCHEMA")
    assert SPEC_BY_KEY["max_pph"].flag == "--max-price-per-hour"
