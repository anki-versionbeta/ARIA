"""In-process registry mapping an opaque download id to a generated file on disk.

Handing the client the generated file's absolute path and streaming back whatever it asks for is a
path-traversal hole over HTTP — any file the server process can read would become downloadable — so the
path never leaves this process. Ids are random and single-process, which also means they do not survive a
restart: the client re-generates, which costs one run.
"""
from __future__ import annotations

import os
import threading
import uuid

_LOCK = threading.Lock()
_FILES: dict[str, str] = {}
# Bounded so a long-running server cannot grow the map without limit. dicts keep insertion order, so
# the oldest id is the one dropped.
_MAX_ENTRIES = 64


def register(path: str) -> str:
    """Record a generated file and return the id a client uses to fetch it."""
    token = REDACTED
    with _LOCK:
        _FILES[token] = os.path.abspath(path)
        while len(_FILES) > _MAX_ENTRIES:
            _FILES.pop(next(iter(_FILES)))
    return token


def resolve(token: str) -> str | None:
    """The path behind an id, or None when the id is unknown or the file has since gone."""
    with _LOCK:
        path = _FILES.get(token)
    return path if (path and os.path.exists(path)) else None
