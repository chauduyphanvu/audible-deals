"""Flask web UI for Audible deal finder."""

from __future__ import annotations

import os

from flask import Flask

from audible_deals.client import AUTH_FILE, LOCALE_CURRENCY, LOCALE_DOMAIN
from audible_deals.display import discount_str, price_str, rating_str


def create_app(locale: str = "us") -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__)
    app.config["LOCALE"] = locale
    app.config["SECRET_KEY"] = os.urandom(24)
    app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1MB upload limit

    # Register blueprints
    from .routes import auth, categories, compare, config, history, library, profiles, search, watch, wishlist

    app.register_blueprint(search.bp)
    app.register_blueprint(library.bp)
    app.register_blueprint(wishlist.bp)
    app.register_blueprint(watch.bp)
    app.register_blueprint(compare.bp)
    app.register_blueprint(history.bp)
    app.register_blueprint(profiles.bp)
    app.register_blueprint(config.bp)
    app.register_blueprint(categories.bp)
    app.register_blueprint(auth.bp)

    # Jinja2 globals
    app.jinja_env.globals.update(
        locale_currency=LOCALE_CURRENCY,
        locale_domain=LOCALE_DOMAIN,
    )

    # Template filters
    app.jinja_env.filters["price"] = lambda p, cur="$": price_str(p, cur)
    app.jinja_env.filters["rating"] = lambda r, n=0: rating_str(r, n)
    app.jinja_env.filters["discount"] = discount_str

    # Auth guard
    @app.before_request
    def _check_auth():
        from flask import request, redirect, url_for

        exempt = {
            "auth.login_page", "auth.login_submit",
            "auth.external_page", "auth.external_callback",
            "auth.import_page", "auth.import_submit",
            "static",
        }
        if request.endpoint in exempt:
            return None
        if not AUTH_FILE.exists():
            return redirect(url_for("auth.login_page"))
        return None

    return app
