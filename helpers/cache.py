"""
OmniRoute - persistent model-list cache for the dashboard.

Stores the most recent full model snapshot (already classified by tier)
inside the plugin's own `config.json` under the `models_cache` key, so
the dashboard can render instantly on page load and degrade gracefully
when the live gateway is unreachable. The cache is a **best-effort
accelerator, not a source of truth** - the live gateway is always tried
first, and a missing, corrupt, version-mismatched, or wrong-base-url
cache is silently ignored.

Storage is **plugin-local** and **shared with `last_known`**:
  - Lives alongside the user config + `last_known` key in
    `usr/plugins/omniroute/config.json`.
  - Uses the same atomic write helper (`_write_raw_config`) as
    `last_known.py` - one writer, one tmp+rename flow.
  - Read-modify-write pattern preserves every key the user (or another
    helper) put there. We never delete a key we didn't put.
  - Tolerates a missing or corrupt file (returns None, logs).
  - A stale or invalid cache never causes an API call to fail.

This module must NOT import anything from `helpers.omniroute_client`
(those are async / blocking HTTP code) and must NOT write outside the
plugin's own folder. Both are guarded by AGENTS.md invariants.

The cache format is **versioned**: bumping `CACHE_FORMAT_VERSION` causes
all older caches to be ignored, giving us a clean migration seam for
future shape changes.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from usr.plugins.omniroute.helpers.last_known import (  # type: ignore
    _read_raw_config,
    _write_raw_config,
)

log = logging.getLogger(__name__)

PLUGIN_NAME = "omniroute"

# Bump this when the cache shape changes. Older caches are silently
# discarded by read_cache() so a deployment doesn't have to ship a
# migration script.
CACHE_FORMAT_VERSION = 1


# ---------------------------------------------------------------------- read


def read_cache() -> Optional[Dict[str, Any]]:
    """Return the stored `models_cache` snapshot, or None on any failure.

    Validates:
      - top-level value is a dict
      - `version` matches `CACHE_FORMAT_VERSION`
      - `models` is a list

    On any failure (missing file, corrupt JSON, version mismatch, wrong
    type) returns None and logs a WARNING. The cache is a best-effort
    accelerator; never raise from this function.
    """
    cfg = _read_raw_config()
    snap = cfg.get("models_cache")
    if not isinstance(snap, dict):
        return None
    if snap.get("version") != CACHE_FORMAT_VERSION:
        log.warning(
            "[omniroute] cache version mismatch: have %r, want %r; ignoring",
            snap.get("version"),
            CACHE_FORMAT_VERSION,
        )
        return None
    if not isinstance(snap.get("models"), list):
        log.warning("[omniroute] cache 'models' is not a list; ignoring")
        return None
    return snap


# --------------------------------------------------------------------- write


def _compute_tier_counts(
    models: List[Dict[str, str]],
) -> Dict[str, int]:
    """Bucket [{id, tier}, ...] into {free, cheap, key, sub} counts.

    Mirrors the single-sourced `count_by_tier` in `omniroute_client.py`
    but does NOT import it (this module is intentionally HTTP-free per
    the module docstring). If a model is missing the `tier` key it
    counts toward the "sub" bucket as the conservative default.
    """
    counts = {"free": 0, "cheap": 0, "key": 0, "sub": 0}
    for m in models:
        tier = m.get("tier") if isinstance(m, dict) else None
        if tier in counts:
            counts[tier] += 1
        else:
            counts["sub"] += 1
    return counts


def write_cache(snapshot: Dict[str, Any]) -> bool:
    """Persist a new `models_cache` snapshot. Returns True on success.

    Required caller keys:
      base_url (str)                                # used to reject cross-URL fallback
      models   (List[Dict[str, str]])               # already classified [{id, tier}, ...]

    Computed here (don't trust caller):
      version, saved_at, saved_at_unix, tier_counts, provider_count

    Merges with the existing config.json so user-set keys AND the
    `last_known` key are preserved. Uses the same atomic tmp+rename
    writer as `last_known.py` for a single source of truth on disk
    safety.
    """
    if not isinstance(snapshot, dict):
        log.warning("[omniroute] write_cache ignored: snapshot is not a dict")
        return False
    base_url = str(snapshot.get("base_url") or "").rstrip("/")
    models_in = snapshot.get("models")
    if not isinstance(models_in, list):
        log.warning("[omniroute] write_cache ignored: 'models' is not a list")
        return False

    # Normalize the model list: keep only dicts with an `id` key. Any
    # other shape is dropped (and the count is computed off the survivors).
    models: List[Dict[str, str]] = []
    for m in models_in:
        if isinstance(m, dict) and m.get("id"):
            tier = m.get("tier")
            if tier not in ("free", "cheap", "key", "sub"):
                tier = "sub"
            models.append({"id": str(m["id"]), "tier": tier})
    counts = _compute_tier_counts(models)
    now = time.time()

    cfg = _read_raw_config()
    cfg["models_cache"] = {
        "version": CACHE_FORMAT_VERSION,
        "saved_at": now,
        "saved_at_unix": now,
        "base_url": base_url,
        "models": models,
        "tier_counts": counts,
        "provider_count": len(models),
    }
    try:
        _write_raw_config(cfg)
        return True
    except OSError as e:
        log.warning("[omniroute] failed to persist models_cache: %s", e)
        return False


# --------------------------------------------------------------------- utils


def cache_age_seconds(snapshot: Optional[Dict[str, Any]]) -> Optional[int]:
    """Seconds since `saved_at_unix`, or None. Mirrors `last_known_age_seconds`."""
    if not snapshot or "saved_at_unix" not in snapshot:
        return None
    try:
        return max(0, int(time.time() - float(snapshot["saved_at_unix"])))
    except (TypeError, ValueError):
        return None


def is_cache_fresh(snapshot: Optional[Dict[str, Any]], ttl_seconds: int) -> bool:
    """True iff snapshot exists and age_seconds < ttl_seconds.

    `ttl_seconds <= 0` disables freshness (any non-None snapshot is
    'fresh' - used by tests and by callers that want to bypass the
    freshness check entirely).
    """
    if not snapshot:
        return False
    if ttl_seconds <= 0:
        return True
    age = cache_age_seconds(snapshot)
    if age is None:
        return False
    return age < ttl_seconds
