"""
storage.py — IssueScout local storage.

A small JSON-file store (no external DB server, per the spec) for:
  * issues already shown to the user, so repeat searches can hide or
    de-emphasize them.
  * lightweight per-profile result caching is left to github_client's
    on-disk HTTP cache; this module only tracks "seen" state.

Single JSON file, safe to delete to reset. Keyed by issue id.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterable

STORE_PATH = Path(os.environ.get("ISSUESCOUT_STORE", ".issuescout_store.json"))


def _load() -> dict:
    try:
        return json.loads(STORE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"seen": {}}


def _save(data: dict) -> None:
    try:
        STORE_PATH.write_text(json.dumps(data))
    except OSError:
        pass  # best-effort; never break the app over storage


def mark_seen(issue_ids: Iterable[int]) -> None:
    """Record that these issue ids have been shown to the user."""
    data = _load()
    seen = data.setdefault("seen", {})
    now = time.time()
    for iid in issue_ids:
        seen[str(iid)] = now
    _save(data)


def get_seen() -> set[int]:
    """Return the set of issue ids already shown."""
    data = _load()
    out = set()
    for k in data.get("seen", {}):
        try:
            out.add(int(k))
        except (TypeError, ValueError):
            continue
    return out


def is_seen(issue_id: int) -> bool:
    return issue_id in get_seen()


def clear_seen() -> None:
    """Forget all seen issues (reset)."""
    _save({"seen": {}})