"""Optional OS-keychain storage for secrets: the login and webhook URLs/headers.

By default everything lives in files in the data directory, readable only by
you (``auth.json`` is mode 600). ``deals config set secret-storage keyring``
moves the secrets into the operating system's keychain (macOS Keychain, Windows
Credential Manager, the Secret Service on Linux) through the optional ``keyring``
package, and leaves *references* in the files: ``auth.json`` holds
``{"secret_storage": "keyring", ...}`` and a webhook setting holds the text
``keyring:<name>``. Readers recognize a reference by its shape, never by the
mode setting, so a half-finished switch still works; the setting only decides
where new secrets are written.

``keyring`` is imported only here and only when a secret is actually read or
written, so the default (file) mode never imports it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)

SERVICE = "audible-deals"
REF_PREFIX = "keyring:"
MODES = ("file", "keyring")

AUTH_ACCOUNT = "auth"
WEBHOOK_ACCOUNT = "webhook"
HEADERS_ACCOUNT = "webhook_headers"

#: Windows Credential Manager refuses secrets over 2560 bytes (an auth file is a
#: few KB), so larger values are split. Each chunk stays well under that in
#: UTF-16, the encoding it uses.
_CHUNK_BYTES = 2000
_CHUNKED = "\x00chunked:"

INSTALL_HINT = (
    "Install the optional keyring support (pip install 'audible-deals[keyring]', "
    "or uv tool install 'audible-deals[keyring]')"
)
SWITCH_BACK_HINT = "or go back to files with: deals config set secret-storage file"


class SecretStoreError(RuntimeError):
    """The keychain could not be used; the message says what to do."""


# -- the keyring package ---------------------------------------------------------


def _backend():
    """The active keyring backend, or :class:`SecretStoreError` saying why not."""
    try:
        import keyring
        from keyring.backends import fail
    except ImportError as exc:
        if getattr(sys, "frozen", False):
            raise SecretStoreError(
                "Secret storage is set to 'keyring', but the pre-built binary does "
                "not include keyring support. Install deals with "
                "'uv tool install 'audible-deals[keyring]'' instead, "
                f"{SWITCH_BACK_HINT}."
            ) from exc
        raise SecretStoreError(
            f"Secret storage is set to 'keyring' but the keyring package is not "
            f"installed. {INSTALL_HINT}, {SWITCH_BACK_HINT}."
        ) from exc
    backend = keyring.get_keyring()
    if isinstance(backend, fail.Keyring) or getattr(backend, "priority", 1) <= 0:
        raise SecretStoreError(
            "Secret storage is set to 'keyring' but no usable keychain was found "
            "(no desktop keychain or Secret Service on this machine, such as a "
            f"headless server). Use one that has a keychain, {SWITCH_BACK_HINT}."
        )
    return backend


def ensure_available() -> None:
    """Raise :class:`SecretStoreError` (with the fix) unless a keychain is usable."""
    _backend()


def backend_name() -> str:
    """A short name for the active keychain (``deals doctor``)."""
    backend = _backend()
    return f"{type(backend).__module__}.{type(backend).__name__}"


def available() -> tuple[bool, str]:
    """``(usable, detail)``: whether secrets can be stored, and the backend or why not."""
    try:
        return True, backend_name()
    except SecretStoreError as exc:
        return False, str(exc)


def _call(operation: str, function, *args):
    backend = _backend()
    try:
        return function(backend, *args)
    except SecretStoreError:
        raise
    except Exception as exc:  # keyring raises many backend-specific errors
        raise SecretStoreError(
            f"The keychain refused to {operation}: {type(exc).__name__}: {exc}. "
            f"If it is locked or asking for approval, unlock it and retry; "
            f"{SWITCH_BACK_HINT}."
        ) from exc


# -- raw items -------------------------------------------------------------------


def _get_item(account: str) -> str | None:
    return _call("read a secret", lambda b, a: b.get_password(SERVICE, a), account)


def _set_item(account: str, value: str) -> None:
    _call(
        "store a secret", lambda b, a, v: b.set_password(SERVICE, a, v), account, value
    )


def _delete_item(account: str) -> None:
    def delete(backend, name):
        try:
            backend.delete_password(SERVICE, name)
        except Exception as exc:
            # keyring.errors.PasswordDeleteError: nothing to delete is fine
            if type(exc).__name__ != "PasswordDeleteError":
                raise

    _call("remove a secret", delete, account)


def _split(value: str) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for char in value:
        width = len(char.encode("utf-16-le"))
        if size + width > _CHUNK_BYTES and current:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += width
    if current:
        chunks.append("".join(current))
    return chunks


def _chunk_account(account: str, ident: str, index: int) -> str:
    return f"{account}#{ident}.{index}"


def _manifest(raw: str | None) -> dict[str, Any] | None:
    if raw is None or not raw.startswith(_CHUNKED):
        return None
    try:
        data = json.loads(raw[len(_CHUNKED) :])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# -- values ----------------------------------------------------------------------


def get(account: str) -> str | None:
    """The secret stored as *account*, or ``None`` when there is none."""
    raw = _get_item(account)
    manifest = _manifest(raw)
    if manifest is None:
        return raw
    try:
        count = int(manifest["n"])
        ident = str(manifest["id"])
        digest = str(manifest["sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SecretStoreError(
            f"The keychain entry '{account}' is damaged. Run 'deals login' again "
            "(or re-enter the webhook)."
        ) from exc
    parts = [_get_item(_chunk_account(account, ident, i)) for i in range(count)]
    value = "".join(part or "" for part in parts)
    if any(part is None for part in parts) or (
        hashlib.sha256(value.encode("utf-8")).hexdigest() != digest
    ):
        raise SecretStoreError(
            f"The keychain entry '{account}' is incomplete or damaged. "
            "Run 'deals login' again (or re-enter the webhook)."
        )
    return value


def put(account: str, value: str) -> None:
    """Store *value* as *account* (replacing it), splitting a large value."""
    previous = _manifest(_get_item(account))
    if len(value.encode("utf-16-le")) <= _CHUNK_BYTES:
        _set_item(account, value)
    else:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        ident = digest[:8]
        chunks = _split(value)
        for index, chunk in enumerate(chunks):
            _set_item(_chunk_account(account, ident, index), chunk)
        manifest = {"n": len(chunks), "id": ident, "sha256": digest}
        _set_item(account, _CHUNKED + json.dumps(manifest))
    if previous is not None:
        _drop_chunks(account, previous, keep=_manifest(_get_item(account)))


def _drop_chunks(
    account: str, manifest: dict[str, Any], keep: dict[str, Any] | None = None
) -> None:
    if keep is not None and keep.get("id") == manifest.get("id"):
        return
    try:
        for index in range(int(manifest["n"])):
            _delete_item(_chunk_account(account, str(manifest["id"]), index))
    except (KeyError, TypeError, ValueError, SecretStoreError) as exc:
        logger.debug("could not remove old keychain chunks of %s: %s", account, exc)


def remove(account: str) -> None:
    """Delete *account* from the keychain (nothing to delete is fine)."""
    manifest = _manifest(_get_item(account))
    _delete_item(account)
    if manifest is not None:
        _drop_chunks(account, manifest)


# -- references ------------------------------------------------------------------


def reference(account: str) -> str:
    return f"{REF_PREFIX}{account}"


def reference_account(value: object) -> str | None:
    """The account a ``keyring:<account>`` reference names, else ``None``."""
    if isinstance(value, str) and value.startswith(REF_PREFIX):
        return value[len(REF_PREFIX) :] or None
    return None


def is_reference(value: object) -> bool:
    return reference_account(value) is not None


def resolve(value: Any) -> Any:
    """*value* with a keychain reference replaced by the secret it names.

    A plain value (every value, in file mode) is returned unchanged, so callers
    can pass whatever the file held. A list holding one reference (the headers
    setting) resolves to the list that was stored.
    """
    account = reference_account(value)
    if account is not None:
        secret = get(account)
        if secret is None:
            raise SecretStoreError(
                f"The keychain has no entry '{account}' (it may have been deleted). "
                "Set the value again, or run 'deals doctor'."
            )
        return secret
    if isinstance(value, list) and len(value) == 1:
        account = reference_account(value[0])
        if account is not None:
            secret = get(account)
            if secret is None:
                raise SecretStoreError(
                    f"The keychain has no entry '{account}' (it may have been "
                    "deleted). Set the value again, or run 'deals doctor'."
                )
            loaded = json.loads(secret)
            return loaded if isinstance(loaded, list) else [loaded]
    return value


def config_references(config: dict) -> list[str]:
    """The secret settings of *config* whose value is a keychain reference."""
    found = []
    for key in ("webhook", "webhook_headers"):
        value = config.get(key)
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        if is_reference(value):
            found.append(key)
    return found


def monitor_account(name: str) -> str:
    return f"monitor:{name}:webhook"


# -- the mode --------------------------------------------------------------------


def mode_of(config: dict | None) -> str:
    """``keyring`` when the config asks for it, else ``file``."""
    if isinstance(config, dict) and config.get("secret_storage") == "keyring":
        return "keyring"
    return "file"


def configured_mode() -> str:
    from audible_deals.config_store import load_config

    return mode_of(load_config())


def protect(value: Any, account: str, *, mode: str | None = None) -> Any:
    """What to save in a file for the secret *value* (file mode: the value itself).

    In keyring mode the value goes to the keychain and the file gets the
    reference; the headers list is stored as JSON and referenced from a
    one-element list.
    """
    if (mode or configured_mode()) != "keyring" or is_reference(value):
        return value
    if isinstance(value, list):
        if len(value) == 1 and is_reference(value[0]):
            return value
        put(account, json.dumps(value))
        return [reference(account)]
    put(account, str(value))
    return reference(account)


def discard(value: Any) -> None:
    """Delete the keychain entry behind *value* if it is a reference (best effort)."""
    account = reference_account(value)
    if account is None and isinstance(value, list) and len(value) == 1:
        account = reference_account(value[0])
    if account is None:
        return
    try:
        remove(account)
    except SecretStoreError as exc:
        logger.debug("could not remove keychain entry %s: %s", account, exc)
