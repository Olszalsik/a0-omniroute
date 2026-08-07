"""
OmniRoute - backend status endpoint.

Route: POST /api/plugins/omniroute/status

Returns a small health snapshot of the configured OmniRoute gateway:
- reachability + latency
- number of upstream models exposed
- sample model IDs
- configured base_url + auth state
- last-known-good status (from the plugin's own config.json) when the
  live check fails, so the WebUI can show "last seen 3 min ago"

The framework surfaces this to the WebUI status badge injected by
`extensions/webui/page-head/omniroute-status.html`.
"""

import logging
import os
import time
from datetime import datetime, timezone

from helpers.api import ApiHandler  # type: ignore

from usr.plugins.omniroute.helpers.last_known import (  # type: ignore
    last_known_age_seconds,
    read_last_known,
    write_last_known,
)
from usr.plugins.omniroute.helpers.omniroute_client import (  # type: ignore
    OmniRouteClient,
    OmniRouteError,
    health_async,
)


PLUGIN_NAME = "omniroute"

# Server-side cooldown: when the last live check against a base_url failed,
# skip the gateway call for _COOLDOWN_SECONDS and surface the on-disk
# `last_known` snapshot instead. Prevents a downed gateway from tying up
# a worker for the full `timeout_seconds` (default 60s) on every poll.
# Window matches the client's POLL_MS in chat-input-bottom-actions-end/
# omniroute-button.html so the badge recovers within one poll cycle.
_COOLDOWN_SECONDS = 60
_LAST_FAIL: dict[str, float] = {}
_log = logging.getLogger(__name__)


def _format_last_known(snapshot):
    """Augment a stored snapshot with `age_seconds` for the WebUI."""
    if not snapshot:
        return None
    out = dict(snapshot)
    out["age_seconds"] = last_known_age_seconds(snapshot)
    return out


class Status(ApiHandler):
    """Returns OmniRoute connectivity + provider count + plugin metadata."""

    async def process(self, input_data, request):
        base_url, api_key, timeout = _resolve_config()

        client = OmniRouteClient(base_url=base_url, api_key=api_key, timeout=timeout)
        live_ok = False

        # Cooldown check: if the last live check against this base_url failed
        # within the last _COOLDOWN_SECONDS, skip the gateway call entirely
        # and let the existing last_known branch surface the cached snapshot.
        now_ts = time.time()
        last_fail = _LAST_FAIL.get(base_url, 0.0)
        in_cooldown = (now_ts - last_fail) < _COOLDOWN_SECONDS
        if in_cooldown:
            age = now_ts - last_fail
            remaining = max(0, _COOLDOWN_SECONDS - age)
            health = {
                "ok": False,
                "error": (
                    f"cooldown: last live check failed {age:.0f}s ago, "
                    f"retrying in {remaining:.0f}s"
                ),
                "base_url": base_url,
                "latency_ms": None,
                "provider_count": 0,
                "sample_models": [],
                "models": [],
            }
            _log.info(
                "[omniroute] status cooldown active for %s (%.0fs since last fail)",
                base_url,
                age,
            )
        else:
            try:
                health = await health_async(client)
                live_ok = bool(health.get("ok"))
                if live_ok:
                    # Clear the cooldown so a recovered gateway is reflected
                    # on the very next call.
                    _LAST_FAIL.pop(base_url, None)
                else:
                    # health() returned ok=False (e.g. non-2xx). Treat as a
                    # failure for cooldown purposes.
                    _LAST_FAIL[base_url] = now_ts
            except OmniRouteError as e:
                _LAST_FAIL[base_url] = now_ts
                health = {"ok": False, "error": str(e), "base_url": base_url,
                          "latency_ms": None, "provider_count": 0, "sample_models": [],
                          "models": []}

        # health() already returns the full model list from the same /v1/models
        # request — no second call needed.
        models = health.get("models") or []

        # Persist the snapshot whenever the live check succeeded; surface the
        # stored snapshot whenever the live check failed.
        last_known_payload = None
        if live_ok:
            write_last_known({
                "latency_ms": health.get("latency_ms") or 0,
                "provider_count": len(models),
                "base_url": health.get("base_url") or base_url,
                "reachable": True,
            })
        else:
            last_known_payload = _format_last_known(read_last_known())

        here = os.path.dirname(os.path.abspath(__file__))
        plugin_root = os.path.dirname(here)
        required = [
            "plugin.yaml",
            "default_config.yaml",
            "hooks.py",
            "conf/model_providers.yaml",
            "agents/omniroute/agent.yaml",
            "webui/install-omniroute.ps1",
        ]
        files_status = {p: os.path.isfile(os.path.join(plugin_root, p)) for p in required}

        return {
            "plugin": PLUGIN_NAME,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "configured_base_url": base_url,
            "auth_configured": bool(api_key),
            "reachable": live_ok,
            "latency_ms": health.get("latency_ms"),
            "provider_count": len(models) if isinstance(models, list) else health.get("provider_count", 0),
            "sample_models": (models or health.get("sample_models") or [])[:5],
            "files": files_status,
            "error": None if live_ok else health.get("error"),
            "last_known": last_known_payload,
        }


def _resolve_config():
    """Best-effort config resolution. Tries the framework helper first,
    then falls back to reading default_config.yaml directly so the endpoint
    works even when the plugin is mid-install or the helper is unavailable."""
    base_url = "http://host.docker.internal:8080/v1"
    api_key = ""
    timeout = 30
    try:
        from helpers import plugins as plugins_helper  # type: ignore

        cfg = plugins_helper.get_plugin_config(PLUGIN_NAME) or {}
        base_url = cfg.get("base_url") or base_url
        api_key = cfg.get("api_key") or api_key
        try:
            timeout = int(cfg.get("timeout_seconds") or timeout)
        except Exception:
            pass
    except Exception:
        try:
            import yaml  # type: ignore

            here = os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(here, "..", "default_config.yaml"), "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            base_url = cfg.get("base_url") or base_url
            api_key = cfg.get("api_key") or api_key
            try:
                timeout = int(cfg.get("timeout_seconds") or timeout)
            except Exception:
                pass
        except Exception:
            pass
    return base_url, api_key, timeout
