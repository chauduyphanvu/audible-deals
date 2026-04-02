"""Config blueprint — manage global CLI defaults."""

from __future__ import annotations

import click
from flask import Blueprint, render_template, request

from audible_deals.cli import (
    _CONFIG_SCHEMA,
    _coerce_config_value,
    _load_config,
    _save_config,
    _validate_config_key,
)

bp = Blueprint("config", __name__)


@bp.get("/config")
def config_page():
    cfg = _load_config()
    return render_template(
        "config.html",
        config=cfg,
        schema=_CONFIG_SCHEMA,
        active_page="config",
    )


@bp.post("/hx/config/set")
def config_set():
    key = request.form.get("key", "").strip()
    value = request.form.get("value", "").strip()
    error = None
    cfg = _load_config()
    if key and value:
        try:
            norm_key = _validate_config_key(key)
            coerced = _coerce_config_value(norm_key, value)
            cfg[norm_key] = coerced
            _save_config(cfg)
        except click.ClickException as exc:
            error = exc.format_message()
        except Exception as exc:
            error = str(exc)
    return render_template(
        "partials/_config_list.html",
        config=cfg,
        schema=_CONFIG_SCHEMA,
        error=error,
    )


@bp.delete("/hx/config/<key>")
def config_delete(key: str):
    cfg = _load_config()
    try:
        norm_key = _validate_config_key(key)
    except click.ClickException as exc:
        return render_template(
            "partials/_config_list.html",
            config=cfg, schema=_CONFIG_SCHEMA, error=exc.format_message(),
        ), 400
    cfg.pop(norm_key, None)
    _save_config(cfg)
    return render_template(
        "partials/_config_list.html",
        config=cfg,
        schema=_CONFIG_SCHEMA,
        error=None,
    )


@bp.post("/hx/config/reset")
def config_reset():
    _save_config({})
    return render_template(
        "partials/_config_list.html",
        config={},
        schema=_CONFIG_SCHEMA,
        error=None,
    )
