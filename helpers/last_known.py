"""
OmniRoute - last-known-good status persistence.

This helper saves a small snapshot of the most recent successful
OmniRoute health check into the plugin's own `config.json`. The
goal is to give the user "last seen 3 min ago, latency 240ms" feedback
when the gateway is currently unreachable — so the WebUI badge
stays meaningful even during a transient outage.

Storage is **plugin-local**:
  - Reads from / writes to `usr/plugins/omniroute/config.json` only.
  - Tolerates a missing or corrupt file (returns None, logs).
  - Writes are atomic: serialize the new dict to a tmp file, then
    rename — a power loss or concurrent write can't leave a
    half-written JSON file.
  - The `last_known` key is added/preserved alongside the user's
    config keys; the helper NEVER deletes or rewrites keys it
    didn't put there.

This module must NOT import anything from `helpers.omniroute_client`
(those are async / blocking HTTP code) and must NOT write outside
the plugin's own folder. Both are guarded by AGENTS.md invariants.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

PLUGIN_NAME = "omniroute"

# Where the user's `config.json` lives. The framework's
# `plugins.get_plugin_config(name)` reads this same file but merges
# defaults in memory; we read/write the raw file here to preserve
# the `last_known` key the framework doesn't know about.
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_PATH = os.path.join(_PLUGIN_DIR, "config.json")


def _read_raw_config() -> Dict[str, Any]:
    """Read the raw `config.json`. Returns {} on any failure."""
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            log.warning("[omniroute] config.json is not a JSON object; ignoring")
            return {}
        return data
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        log.warning("[omniroute] failed to read config.json: %s", e)
        return {}


def _write_raw_config(data: Dict[str, Any]) -> None:
    """Write atomically: tmp file + os.replace(). Never leaves a half-written JSON."""
    dir_name = os.path.dirname(_CONFIG_PATH)
    fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=dir_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, _CONFIG_PATH)
    except Exception:
        # Clean up the tmp file on any failure
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_last_known() -> Optional[Dict[str, Any]]:
    """Return the stored `last_known` snapshot, or None if missing/invalid.

    Shape:
      {
        "ts": "2026-07-18T12:34:56.789Z",   # ISO-8601 UTC
        "latency_ms": 240,
        "provider_count": 187,
        "base_url": "http://host.docker.internal:8080/v1",
        "reachable": True,
        "age_seconds": 0      # populated by the consumer (not stored)
      }
    """
    cfg = _read_raw_config()
    lk = cfg.get("last_known")
    if not isinstance(lk, dict):
        return None
    return lk


def write_last_known(snapshot: Dict[str, Any]) -> bool:
    """Persist a new `last_known` snapshot. Returns True on success.

    Merges with the existing `config.json` so user-set keys (base_url,
    api_key, default_model, etc.) are preserved. Sets the `last_known`
    key under a `_ts` Unix timestamp and a `ts_iso` human-readable
    timestamp.

    Accepts these keys from the caller:
      latency_ms (int), provider_count (int), base_url (str), reachable (bool)
    """
    if not isinstance(snapshot, dict):
        log.warning("[omniroute] write_last_known ignored: snapshot is not a dict")
        return False

    cfg = _read_raw_config()
    cfg["last_known"] = {
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "latency_ms": int(snapshot.get("latency_ms") or 0),
        "provider_count": int(snapshot.get("provider_count") or 0),
        "base_url": str(snapshot.get("base_url") or ""),
        "reachable": bool(snapshot.get("reachable", True)),
    }
    try:
        _write_raw_config(cfg)
        return True
    except OSError as e:
        log.warning("[omniroute] failed to persist last_known: %s", e)
        return False


def last_known_age_seconds(snapshot: Optional[Dict[str, Any]]) -> Optional[int]:
    """Helper for the WebUI: return seconds since the snapshot was taken, or None."""
    if not snapshot or "ts" not in snapshot:
        return None
    try:
        return max(0, int(time.time() - float(snapshot["ts"])))
    except (TypeError, ValueError):
        return None
