"""
OmniRoute - dashboard data aggregator.

Route: POST /api/plugins/omniroute/dashboard

Aggregates status + models into a single response suitable for the
dashboard UI. Tier classification (free/cheap/key/sub) is delegated
to `helpers.omniroute_client.classify_tier` so both this handler and
the usage analytics handler share the same heuristic.

Cache integration (Phase 5.1):
  The handler always tries the live gateway first. On success, the
  full model snapshot is persisted to `config.json["models_cache"]` so
  the next request (or the next page load) can render instantly even
  if the live gateway is unreachable. The cache is a best-effort
  accelerator, not a source of truth:
    - A missing, corrupt, version-mismatched, or wrong-base-url cache
      is silently ignored; the response is still built from the live
      data (or the all-zero offline envelope if the live call failed).
    - A failed cache write never breaks the live response.
    - A stale cache from a different gateway is never served to the
      user (the URL-mismatch guard).

This is a best-effort heuristic. The exact tier can be confirmed via
OmniRoute's dashboard at the configured base URL.
"""

import logging
import os
from typing import Any, Dict, List, Optional

from helpers.api import ApiHandler  # type: ignore

from usr.plugins.omniroute.helpers.cache import (  # type: ignore
    cache_age_seconds,
    is_cache_fresh,
    read_cache,
    write_cache,
)
from usr.plugins.omniroute.helpers.omniroute_client import (  # type: ignore
    OmniRouteClient,
    OmniRouteError,
    classify_tier,
    count_by_tier,
    health_async,
    tier_sort_key,
)


PLUGIN_NAME = "omniroute"
log = logging.getLogger(__name__)

# Default cache TTL (seconds). Overridden by `cache_ttl_seconds` in
# the user's config.json / .a0proj scopes / default_config.yaml.
_DEFAULT_CACHE_TTL_SECONDS = 3600  # 1 hour


def _resolve_config():
    """Best-effort config resolution, mirrors the other handlers.

    Returns ``(base_url, api_key, timeout, cache_ttl_seconds)``. The
    cache TTL is read through the same `cfg.get(...)` path as
    `timeout_seconds`, with a 1-hour default if the key is missing or
    unparseable so older config.json files keep working.
    """
    base_url = "http://host.docker.internal:8080/v1"
    api_key = ""
    timeout = 30
    cache_ttl_seconds = _DEFAULT_CACHE_TTL_SECONDS
    try:
        from helpers import plugins as plugins_helper  # type: ignore
        cfg = plugins_helper.get_plugin_config(PLUGIN_NAME) or {}
        base_url = cfg.get("base_url") or base_url
        api_key = cfg.get("api_key") or api_key
        try:
            timeout = int(cfg.get("timeout_seconds") or timeout)
        except Exception:
            pass
        try:
            cache_ttl_seconds = int(cfg.get("cache_ttl_seconds") or cache_ttl_seconds)
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
            try:
                cache_ttl_seconds = int(cfg.get("cache_ttl_seconds") or cache_ttl_seconds)
            except Exception:
                pass
        except Exception:
            pass
    return base_url, api_key, timeout, cache_ttl_seconds


def _format_cache_for_response(snapshot: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Add the computed `age_seconds` to a cache snapshot for the UI.

    Returns None if the snapshot is falsy. Otherwise shallow-copies it
    and adds `age_seconds` so the frontend doesn't have to compute it.
    """
    if not snapshot:
        return None
    out = dict(snapshot)
    out["age_seconds"] = cache_age_seconds(snapshot)
    return out


class Dashboard(ApiHandler):
    """Aggregated dashboard data: status + models with tier classification.

    Response fields:
      reachable (bool)              - True if the live gateway returned a 2xx
      error (str|None)              - error message on failure
      base_url (str)                - the URL we attempted
      provider_count (int)          - total models in the response
      free_count/cheap_count/key_count/sub_count (int)
      latency_ms (int)              - measured live; 0 on cache-only
      models (list)                 - [{id, tier}, ...]
      from_cache (bool)             - True iff this response came from the cache
                                     (the live call failed AND the cache's
                                     base_url matched)
      cached_snapshot (dict|None)   - the on-disk cache (or None), with an
                                     added age_seconds for the UI pill
    """

    async def process(self, input_data, request):
        base_url, api_key, timeout, cache_ttl_seconds = _resolve_config()
        client = OmniRouteClient(base_url=base_url, api_key=api_key, timeout=timeout)

        # 1. Read the cache up front so it's available even if the live
        #    call fails. The cache is informational here — we still
        #    always try the live gateway (the cache is an accelerator,
        #    not a gate).
        cached = read_cache()
        cache_fresh = is_cache_fresh(cached, cache_ttl_seconds)

        # 2. Try the live gateway.
        try:
            health = await health_async(client)
        except OmniRouteError as e:
            health = {
                "ok": False,
                "error": str(e),
                "base_url": base_url,
                "latency_ms": None,
                "provider_count": 0,
                "models": [],
            }

        live_ok = bool(health.get("ok"))

        # 3. Live succeeded. Build the response from live data and
        #    persist the snapshot for next time. The write is wrapped
        #    in try/except so a cache failure never breaks the response.
        if live_ok:
            raw_models = health.get("models") or []
            classified: List[Dict[str, str]] = [
                {"id": mid, "tier": classify_tier(mid)} for mid in raw_models
            ]
            # Sort: free first, then cheap, key, sub; alphabetical within tier
            classified.sort(key=tier_sort_key)
            counts = count_by_tier(raw_models)

            # Reject cross-URL writes: a cache from a different gateway
            # is a separate thing and shouldn't be silently overwritten.
            # Defensive — the typical case is `cached is None` on first run.
            if cached is None or cached.get("base_url") == base_url:
                try:
                    write_cache({"base_url": base_url, "models": classified})
                except Exception as e:  # pragma: no cover
                    log.debug("[%s] cache write failed (non-fatal): %s", PLUGIN_NAME, e)

            return {
                "reachable": True,
                "base_url": health.get("base_url") or base_url,
                "latency_ms": health.get("latency_ms"),
                "provider_count": len(classified),
                "free_count": counts["free"],
                "cheap_count": counts["cheap"],
                "key_count": counts["key"],
                "sub_count": counts["sub"],
                "models": classified,
                "from_cache": False,
                "cached_snapshot": _format_cache_for_response(cached),
            }

        # 4. Live call failed. Fall back to the cache if it exists AND
        #    its base_url matches the currently configured one (don't
        #    serve a stale cache from a gateway the user used
        #    previously).
        if cached and cached.get("base_url") == base_url:
            counts = cached.get("tier_counts") or {
                "free": 0, "cheap": 0, "key": 0, "sub": 0
            }
            models = cached.get("models") or []
            return {
                "reachable": False,
                "error": health.get("error"),
                "base_url": base_url,
                "latency_ms": 0,
                "provider_count": len(models),
                "free_count": counts.get("free", 0),
                "cheap_count": counts.get("cheap", 0),
                "key_count": counts.get("key", 0),
                "sub_count": counts.get("sub", 0),
                "models": models,
                "from_cache": True,
                "cached_snapshot": _format_cache_for_response(cached),
            }

        # 5. No live data AND no usable cache — return the original
        #    offline envelope. `from_cache: False` is honest; the UI
        #    shows "Offline".
        return {
            "reachable": False,
            "error": health.get("error"),
            "base_url": base_url,
            "provider_count": 0,
            "free_count": 0, "cheap_count": 0, "key_count": 0, "sub_count": 0,
            "latency_ms": 0,
            "models": [],
            "from_cache": False,
            "cached_snapshot": None,
        }
