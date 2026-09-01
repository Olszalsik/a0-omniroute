"""
OmniRoute - model listing endpoint.

Route: POST /api/plugins/omniroute/models

Proxies OmniRoute's /v1/models and returns a curated list suitable for
Agent Zero's model picker. Useful when the WebUI wants to refresh the
model dropdown without reloading the page.

Response shape (intentionally matches `api/dashboard.py` so every UI
can render tier coloring with one helper):
  {
    "count": N,
    "models":  [{"id": "...", "tier": "free|cheap|key|sub"}, ...],
    "filtered": [...same shape, after the user's filter is applied...],
    "filter": "<user's substring filter, or empty string>",
    "base_url": "...",
    "tier_counts": {"free": N, "cheap": N, "key": N, "sub": N},
    "error": null | "<message>"
  }

On failure (OmniRoute unreachable) returns:
  { "count": 0, "models": [], "filtered": [], "filter": "",
    "base_url": "...", "tier_counts": {"free":0,"cheap":0,"key":0,"sub":0},
    "error": "..." }

The list is sorted free -> cheap -> key -> sub, alphabetical within
each tier, so UIs that render without an explicit sort get a sane
ordering.

Input (optional):
  { "filter": "claude" }   # substring filter, case-insensitive
  { "tier":   "free"   }   # tier filter, one of "free" | "cheap" | "key" | "sub";
                            # anything else (including "" or absent) returns all
                            # tiers. The substring and tier filters compose with
                            # AND, so {"filter": "claude", "tier": "free"} matches
                            # free claude models. This is the v1.4.0 entry point
                            # for the WebUI tier dropdown — see
                            # ``usr/plugins/omniroute/extensions/webui/chat-input-
                            # bottom-actions-end/tier-filter.html``.
"""

import os

from helpers.api import ApiHandler  # type: ignore

from usr.plugins.omniroute.helpers.omniroute_client import (  # type: ignore
    OmniRouteClient,
    OmniRouteError,
    classify_tier,
    count_by_tier,
    health_async,
    tier_sort_key,
)


PLUGIN_NAME = "omniroute"


def _build_list(raw_models):
    """Classify + sort the raw model ids into [{id, tier}, ...] shape.

    Reused by the dashboard handler (kept here as a private helper
    because `count_by_tier` + `classify_tier` are already the single
    source of truth — this just composes them into the list shape).
    """
    classified = [{"id": mid, "tier": classify_tier(mid)} for mid in raw_models]
    classified.sort(key=tier_sort_key)
    return classified


# v1.4.0 — tier filter for the WebUI dropdown. Keeps the set local
# to this module so the WebUI can rely on the canonical list.
_TIERS = frozenset({"free", "cheap", "key", "sub"})


def _apply_tier_filter(items, tier):
    """Return items whose ``tier`` matches the given tier, or the
    original list when ``tier`` is empty / unknown. Items missing the
    ``tier`` key are dropped when a tier filter is active (the
    `_build_list` helper always sets it, so this only matters for
    callers that build their own list shape)."""
    if not tier or tier not in _TIERS:
        return items
    return [m for m in items if m.get("tier") == tier]


def _empty_response(base_url: str, error: str = "", flt: str = "", tier: str = "") -> dict:
    return {
        "count": 0,
        "models": [],
        "filtered": [],
        "filter": flt,
        "tier": tier,
        "base_url": base_url,
        "tier_counts": {"free": 0, "cheap": 0, "key": 0, "sub": 0},
        "error": error or None,
    }


class Models(ApiHandler):
    async def process(self, input_data, request):
        base_url, api_key, timeout = _resolve_config()
        client = OmniRouteClient(base_url=base_url, api_key=api_key, timeout=timeout)

        flt = ""
        tier = ""
        if isinstance(input_data, dict):
            flt = (input_data.get("filter") or "").strip().lower()
            tier = (input_data.get("tier") or "").strip().lower()
            if tier not in _TIERS:
                tier = ""

        # health() already returns the full model list (Phase 2 refactor);
        # one GET /v1/models per request, no second call.
        try:
            health = await health_async(client)
        except OmniRouteError as e:
            return _empty_response(base_url, error=str(e), flt=flt, tier=tier)

        if not health.get("ok"):
            return _empty_response(base_url, error=health.get("error") or "gateway not reachable", flt=flt, tier=tier)

        raw_models = health.get("models") or []
        models = _build_list(raw_models)
        counts = count_by_tier(raw_models)

        if flt:
            filtered = [m for m in models if flt in m["id"].lower()]
        else:
            filtered = list(models)
        filtered = _apply_tier_filter(filtered, tier)

        return {
            "count": len(models),
            "models": models,
            "filtered": filtered,
            "filter": flt,
            "tier": tier,
            "base_url": base_url,
            "tier_counts": counts,
            "error": None,
        }


def _resolve_config():
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
        pass
    return base_url, api_key, timeout
