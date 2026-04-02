"""Auth blueprint — login, external OAuth, and auth file import."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import audible
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from audible_deals.client import AUTH_FILE, DealsClient

from . import get_locale

bp = Blueprint("auth", __name__, url_prefix="/auth")


@bp.get("/login")
def login_page():
    return render_template("auth/login.html", external=False)


@bp.post("/login")
def login_submit():
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    if not email or not password:
        flash("Email and password are required.", "error")
        return redirect(url_for("auth.login_page"))
    try:
        with DealsClient(locale=get_locale()) as dc:
            dc.login(email, password)
        flash("Signed in successfully.", "success")
        return redirect(url_for("search.find_page"))
    except Exception as exc:
        flash(f"Login failed: {exc}", "error")
        return redirect(url_for("auth.login_page"))


@bp.get("/login/external")
def external_page():
    return render_template("auth/login.html", external=True)


@bp.post("/login/external/callback")
def external_callback():
    callback_url = request.form.get("callback_url", "").strip()
    if not callback_url:
        flash("Callback URL is required.", "error")
        return redirect(url_for("auth.external_page"))
    try:
        locale = get_locale()
        # Pass the already-submitted callback URL directly to the audible
        # library via login_url_callback. This avoids the blocking input()
        # call inside DealsClient.login_external(), which expects a CLI flow.
        auth = audible.Authenticator.from_login_external(
            locale=locale,
            login_url_callback=lambda _oauth_url: callback_url,
        )
        AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(AUTH_FILE.parent, 0o700)
        auth.to_file(AUTH_FILE)
        os.chmod(AUTH_FILE, 0o600)
        flash("Signed in via external browser successfully.", "success")
        return redirect(url_for("search.find_page"))
    except Exception as exc:
        flash(f"External login failed: {exc}", "error")
        return redirect(url_for("auth.external_page"))


@bp.get("/import")
def import_page():
    return render_template("auth/import.html")


@bp.post("/import")
def import_submit():
    uploaded = request.files.get("auth_file")
    if not uploaded or uploaded.filename == "":
        flash("No file selected.", "error")
        return redirect(url_for("auth.import_page"))
    try:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            temp_path = Path(tmp.name)
        try:
            uploaded.save(temp_path)
            with DealsClient(locale=get_locale()) as dc:
                dc.import_auth(temp_path)
        finally:
            temp_path.unlink(missing_ok=True)
        flash("Auth file imported successfully.", "success")
        return redirect(url_for("search.find_page"))
    except Exception as exc:
        flash(f"Import failed: {exc}", "error")
        return redirect(url_for("auth.import_page"))
