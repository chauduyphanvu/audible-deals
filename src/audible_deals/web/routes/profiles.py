"""Profiles blueprint — manage saved search profiles."""

from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from audible_deals.cli import _load_profiles, _save_profiles

bp = Blueprint("profiles", __name__)

# Filter fields that can be saved in a profile
_PROFILE_FIELDS = [
    "genre", "max_price", "min_rating", "min_ratings", "min_hours",
    "sort", "pages", "narrator", "author",
]
_PROFILE_BOOL_FIELDS = ["on_sale", "deep", "first_in_series", "skip_owned"]


def _render_profile_list(profiles, error=None):
    return render_template("partials/_profile_list.html", profiles=profiles, error=error)


@bp.get("/profiles")
def profiles_page():
    profiles = _load_profiles()
    return render_template("profiles.html", profiles=profiles, active_page="profiles")


@bp.post("/hx/profiles/save")
def profiles_save():
    name = request.form.get("name", "").strip()
    if not name:
        return _render_profile_list(_load_profiles(), error="Profile name is required.")

    opts: dict = {}
    for field in _PROFILE_FIELDS:
        val = request.form.get(field, "").strip()
        if val:
            # Coerce numeric fields
            if field in ("max_price", "min_rating", "min_hours"):
                try:
                    opts[field] = float(val)
                except ValueError:
                    pass
            elif field in ("min_ratings", "pages"):
                try:
                    opts[field] = int(val)
                except ValueError:
                    pass
            else:
                opts[field] = val

    for field in _PROFILE_BOOL_FIELDS:
        val = request.form.get(field)
        if val in ("true", "1", "on"):
            opts[field] = True

    profiles = _load_profiles()
    profiles[name] = opts
    _save_profiles(profiles)
    return _render_profile_list(profiles)


@bp.delete("/hx/profiles/<name>")
def profiles_delete(name: str):
    profiles = _load_profiles()
    profiles.pop(name, None)
    _save_profiles(profiles)
    return _render_profile_list(profiles)


@bp.get("/hx/profiles/<name>")
def profiles_get(name: str):
    profiles = _load_profiles()
    opts = profiles.get(name, {})
    return jsonify(opts)
