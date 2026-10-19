"""The Zenodo access token lives in the OS credential store, never in a file.

Same store as the ELN tokens (:mod:`mmfdb.security.credentials`: ``keyring``
when installed, the macOS Keychain otherwise). One token per repository host,
so the sandbox and production keep separate tokens. ``ZENODO_TOKEN`` in the
environment is the fallback, read at call time and never persisted.

Reading the OS store launches a process (macOS ``security``) that may block on
an unlock prompt, so it happens only when a token is actually needed
(:func:`load_token`, called when a client is built). A GUI that only *shows*
where the token comes from asks :func:`known_token`, which never touches the
store: it answers from what this process already read, stored or deleted, and
says ``"unchecked"`` while the store has not been consulted.
"""

from __future__ import annotations

import os
import urllib.parse

from mmfdb.security import credentials as _store

ENV = "ZENODO_TOKEN"
_USER = "deposit"
#: Source :func:`known_token` reports while the OS store has not been read.
UNCHECKED = "unchecked"
#: What this process learnt from the OS store, per account: the token, or ``None`` for none.
_KNOWN: dict[tuple[str, int], str | None] = {}


def _account(base_url: str) -> tuple[str, int]:
    return f"zenodo-{urllib.parse.urlsplit(base_url).hostname or base_url}", 0


def store_token(base_url: str, token: str) -> bool:
    """Store the token for this repository; ``False`` when no store accepted it."""
    host, port = _account(base_url)
    ok = _store.store_session_token(host, port, _USER, token.strip())
    if ok:
        _KNOWN[(host, port)] = token.strip()
    return ok


def stored_token(base_url: str) -> str | None:
    host, port = _account(base_url)
    tok = _store.load_session_token(host, port, _USER) or None
    _KNOWN[(host, port)] = tok
    return tok


def delete_token(base_url: str) -> bool:
    host, port = _account(base_url)
    ok = _store.delete_session_token(host, port, _USER)
    if ok:
        _KNOWN[(host, port)] = None
    return ok


def load_token(base_url: str) -> tuple[str, str]:
    """``(token, source)``: the stored token, else ``ZENODO_TOKEN``, else ``("", "")``."""
    tok = stored_token(base_url)
    if tok:
        return tok, "keychain"
    tok = os.environ.get(ENV, "")
    return (tok, "environment") if tok else ("", "")


def known_token(base_url: str) -> tuple[str, str]:
    """``(token, source)`` without reading the OS store -- safe to call every frame.

    Answers like :func:`load_token` once this process has read, stored or deleted the token
    for this host. Before that the source is :data:`UNCHECKED` (token ``""``) unless
    ``ZENODO_TOKEN`` is set, which is reported as ``"environment"``.
    """
    account = _account(base_url)
    env = os.environ.get(ENV, "")
    if account not in _KNOWN:
        return (env, "environment") if env else ("", UNCHECKED)
    tok = _KNOWN[account]
    if tok:
        return tok, "keychain"
    return (env, "environment") if env else ("", "")
