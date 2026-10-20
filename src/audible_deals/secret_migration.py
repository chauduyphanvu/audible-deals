"""Moving secrets between the data directory's files and the OS keychain.

``deals config set secret-storage keyring`` and ``... file`` run :func:`migrate`.
The order of steps is what keeps a failure harmless, because readers recognize a
reference by its shape (see :mod:`audible_deals.secret_store`):

* to the keychain: every secret is stored and read back *first*; only then are the
  files rewritten to hold references. A failure before that leaves the files
  exactly as they were.
* to files: every secret is read from the keychain *first* (a missing one stops
  the whole move); the files are written; only then are the keychain entries
  deleted.

Nothing here deletes a file. The login file ``auth.json`` is rewritten in place
(atomically, mode 600): holding only a reference is the point of the move, so the
old plaintext is deliberately not kept beside it. Go back with the other
direction.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from audible_deals import paths, secret_store
from audible_deals.auth_state import auth_reference, parse_auth_reference
from audible_deals.config_store import (
    config_transaction,
    load_config,
    load_monitors,
    load_monitors_for_update,
    save_monitors,
)
from audible_deals.locking import advisory_lock, run_lock
from audible_deals.storage import _atomic_write_bytes, read_text

logger = logging.getLogger(__name__)

_PROBE_ACCOUNT = "probe"


@dataclass(frozen=True)
class Plan:
    """What a switch would move."""

    target: str
    auth: bool = False
    webhook: bool = False
    headers: bool = False
    monitors: tuple[str, ...] = ()

    @property
    def count(self) -> int:
        return sum((self.auth, self.webhook, self.headers)) + len(self.monitors)

    def describe(self) -> list[str]:
        items = []
        if self.auth:
            items.append("your Audible login (auth.json)")
        if self.webhook:
            items.append("the webhook URL")
        if self.headers:
            items.append("the webhook headers")
        items += [f"the webhook of monitor '{name}'" for name in self.monitors]
        return items


@dataclass
class Report:
    target: str
    moved: list[str] = field(default_factory=list)
    leftovers: list[str] = field(default_factory=list)


def _auth_is_reference() -> bool | None:
    """Whether auth.json is a keychain reference; ``None`` when there is no login file."""
    path = paths.auth_file()
    if not path.exists():
        return None
    try:
        return parse_auth_reference(json.loads(read_text(path))) is not None
    except (OSError, ValueError):
        return None


def _webhook_values(config: dict) -> tuple[object, object]:
    return config.get("webhook"), config.get("webhook_headers")


def plan_for(target: str) -> Plan:
    """What moving to *target* storage would touch (nothing is changed)."""
    if target not in secret_store.MODES:
        raise ValueError(f"unknown secret storage {target!r}")
    to_keychain = target == "keyring"
    config = load_config()
    webhook, headers = _webhook_values(config)

    def movable(value: object) -> bool:
        if isinstance(value, list):
            referenced = len(value) == 1 and secret_store.is_reference(value[0])
            return bool(value) and referenced != to_keychain
        if not isinstance(value, str) or not value:
            return False
        return secret_store.is_reference(value) != to_keychain

    monitors = tuple(
        sorted(
            name
            for name, definition in load_monitors().items()
            if isinstance(definition, dict) and movable(definition.get("webhook"))
        )
    )
    auth_is_ref = _auth_is_reference()
    return Plan(
        target=target,
        auth=auth_is_ref is not None and auth_is_ref != to_keychain,
        webhook=movable(webhook),
        headers=movable(headers),
        monitors=monitors,
    )


def _probe() -> None:
    """Fail early, before anything changes, if the keychain cannot store a secret."""
    secret_store.put(_PROBE_ACCOUNT, "probe")
    try:
        if secret_store.get(_PROBE_ACCOUNT) != "probe":
            raise secret_store.SecretStoreError(
                "The keychain did not keep a test secret. Nothing was changed."
            )
    finally:
        secret_store.remove(_PROBE_ACCOUNT)


def _store_verified(account: str, value: str) -> None:
    secret_store.put(account, value)
    if secret_store.get(account) != value:
        raise secret_store.SecretStoreError(
            f"The keychain did not keep '{account}' (reading it back returned "
            "something else). Nothing was changed."
        )


def _auth_lock():
    path = paths.auth_file()
    return advisory_lock(path.with_name(f".{path.name}.lock"), wait=True)


def migrate(target: str) -> Report:
    """Move every secret to *target* storage and record it in the config."""
    plan = plan_for(target)
    if target == "keyring":
        return _to_keychain(plan)
    return _to_files(plan)


def _to_keychain(plan: Plan) -> Report:
    report = Report("keyring")
    _probe()
    config = load_config()
    webhook, headers = _webhook_values(config)

    # 1. Everything into the keychain, verified, before any file changes.
    auth_text = None
    if plan.auth:
        auth_text = read_text(paths.auth_file())
        auth_data = json.loads(auth_text)
        if not isinstance(auth_data, dict):
            raise secret_store.SecretStoreError("auth.json is not a login file.")
        _store_verified(secret_store.AUTH_ACCOUNT, auth_text)
    if plan.webhook and isinstance(webhook, str):
        _store_verified(secret_store.WEBHOOK_ACCOUNT, webhook)
    if plan.headers and isinstance(headers, list):
        _store_verified(secret_store.HEADERS_ACCOUNT, json.dumps(headers))
    monitor_hooks = {name: load_monitors()[name]["webhook"] for name in plan.monitors}
    for name, url in monitor_hooks.items():
        _store_verified(secret_store.monitor_account(name), url)

    # 2. Only now do the files change to hold references.
    with run_lock():
        if auth_text is not None:
            with _auth_lock():
                _atomic_write_bytes(
                    paths.auth_file(),
                    json.dumps(
                        auth_reference(json.loads(auth_text)), indent=2
                    ).encode(),
                )
                paths.auth_file().chmod(0o600)
            report.moved.append("login")
        with config_transaction() as cfg:
            if plan.webhook:
                cfg["webhook"] = secret_store.reference(secret_store.WEBHOOK_ACCOUNT)
                report.moved.append("webhook URL")
            if plan.headers:
                cfg["webhook_headers"] = [
                    secret_store.reference(secret_store.HEADERS_ACCOUNT)
                ]
                report.moved.append("webhook headers")
            cfg["secret_storage"] = "keyring"
        if monitor_hooks:
            monitors = load_monitors_for_update()
            for name in monitor_hooks:
                if name in monitors and isinstance(monitors[name], dict):
                    monitors[name]["webhook"] = secret_store.reference(
                        secret_store.monitor_account(name)
                    )
                    report.moved.append(f"webhook of monitor '{name}'")
            save_monitors(monitors)
    return report


def _referenced(value: object, default: str) -> str:
    """The account the reference *value* (a string or a one-item list) names."""
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    return secret_store.reference_account(value) or default


def _to_files(plan: Plan) -> Report:
    report = Report("file")
    if plan.count:
        secret_store.ensure_available()  # a clear error before touching anything
    config = load_config()
    webhook, headers = _webhook_values(config)

    # 1. Read every secret first; a missing entry stops the whole move.
    auth_text = None
    if plan.auth:
        auth_text = secret_store.get(secret_store.AUTH_ACCOUNT)
        if auth_text is None:
            raise secret_store.SecretStoreError(
                "Your login is not in the keychain any more, so it cannot be moved "
                "back. Run 'deals login' to sign in again."
            )
    webhook_value = secret_store.resolve(webhook) if plan.webhook else None
    headers_value = secret_store.resolve(headers) if plan.headers else None
    stored_monitors = load_monitors()
    monitor_hooks = {
        name: secret_store.resolve(stored_monitors[name]["webhook"])
        for name in plan.monitors
    }
    # The entries to delete afterwards are the ones the references name (a
    # reference edited by hand need not follow the usual account naming).
    accounts = []
    if plan.auth:
        accounts.append(secret_store.AUTH_ACCOUNT)
    if plan.webhook:
        accounts.append(_referenced(webhook, secret_store.WEBHOOK_ACCOUNT))
    if plan.headers:
        accounts.append(_referenced(headers, secret_store.HEADERS_ACCOUNT))
    accounts += [
        _referenced(
            stored_monitors[name]["webhook"], secret_store.monitor_account(name)
        )
        for name in plan.monitors
    ]

    # 2. Write the files.
    with run_lock():
        if auth_text is not None:
            with _auth_lock():
                _atomic_write_bytes(paths.auth_file(), auth_text.encode("utf-8"))
                paths.auth_file().chmod(0o600)
            report.moved.append("login")
        with config_transaction() as cfg:
            if plan.webhook:
                cfg["webhook"] = webhook_value
                report.moved.append("webhook URL")
            if plan.headers:
                cfg["webhook_headers"] = headers_value
                report.moved.append("webhook headers")
            cfg["secret_storage"] = "file"
        if monitor_hooks:
            monitors = load_monitors_for_update()
            for name, url in monitor_hooks.items():
                if name in monitors and isinstance(monitors[name], dict):
                    monitors[name]["webhook"] = url
                    report.moved.append(f"webhook of monitor '{name}'")
            save_monitors(monitors)

    # 3. The keychain copies are no longer referenced: remove them.
    for account in accounts:
        try:
            secret_store.remove(account)
        except secret_store.SecretStoreError as exc:
            logger.debug("could not remove keychain entry %s: %s", account, exc)
            report.leftovers.append(account)
    return report
