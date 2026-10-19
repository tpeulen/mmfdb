"""The Zenodo access token lives in the OS credential store, never in a file.

Same store as the ELN tokens (:mod:`mmfdb.security.credentials`: ``keyring``
when installed, the macOS Keychain otherwise). One token per repository host,
so the sandbox and production keep separate tokens. ``ZENODO_TOKEN`` in the
environment is the fallback, read at call time and never persisted.
"""

from __future__ import annotations

import os
import urllib.parse

from mmfdb.security import credentials as _store

ENV = "ZENODO_TOKEN"
_USER = "deposit"


def _account(base_url: str) -> tuple[str, int]:
    return f"zenodo-{urllib.parse.urlsplit(base_url).hostname or base_url}", 0


def store_token(base_url: str, token: str) -> bool:
    """Store the token for this repository; ``False`` when no store accepted it."""
    host, port = _account(base_url)
    return _store.store_session_token(host, port, _USER, token.strip())


def stored_token(base_url: str) -> str | None:
    host, port = _account(base_url)
    return _store.load_session_token(host, port, _USER) or None


def delete_token(base_url: str) -> bool:
    host, port = _account(base_url)
    return _store.delete_session_token(host, port, _USER)


def load_token(base_url: str) -> tuple[str, str]:
    """``(token, source)``: the stored token, else ``ZENODO_TOKEN``, else ``("", "")``."""
    tok = stored_token(base_url)
    if tok:
        return tok, "keychain"
    tok = os.environ.get(ENV, "")
    return (tok, "environment") if tok else ("", "")
