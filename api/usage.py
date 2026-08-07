"""
OmniRoute - usage analytics proxy.

Route: POST /api/plugins/omniroute/usage

Proxies OmniRoute's usage endpoint (if available) or returns per-tier
analytics computed from the model list. OmniRoute's gateway may not expose
a /v1/usage endpoint publicly, so this handler gracefully degrades
to tier counts derived from the model list when the endpoint is missing.
"""

import os
from typing import Any, Dict, List

from helpers.api import ApiHandler  # type: ignore

from usr.plugins.omniroute.helpers.omniroute_client import (  # type: ignore
    OmniRouteClient,
    OmniRouteError,
    count_by_tier,
    health_async,
    usage_async,
)


PLUGIN_NAME = "omniroute"


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


class Usage(ApiHandler):
    """Returns usage analytics. Best-effort — degrades gracefully when the
    upstream gateway doesn't expose a usage endpoint."""

    async def process(self, input_data, request):
        base_url, api_key, timeout = _resolve_config()
        client = OmniRouteClient(base_url=base_url, api_key=api_key, timeout=timeout)

        # Try the gateway's usage endpoint first
        try:
            r = await usage_async(client)
            body = r.get("body") or {}
            if isinstance(body, dict) and (body.get("total_requests") or body.get("usage")):
                return {
                    "source": "gateway",
                    "reachable": True,
                    "data": body,
                }
        except OmniRouteError:
            pass

        # Fall back to per-tier counts derived from the model list
        try:
            health = await health_async(client)
            if not health.get("ok"):
                return {"source": "unreachable", "reachable": False, "data": {}}
            # health() already returns the full model list — no second call needed
            models = health.get("models") or []
            counts = count_by_tier(models)
            return {
                "source": "derived",
                "reachable": True,
                "data": {
                    "model_counts": counts,
                    "total_models": len(models),
                    "note": "Per-tier counts derived from model list (gateway usage endpoint not exposed)",
                },
            }
        except Exception as e:
            return {"source": "error", "reachable": False, "error": str(e), "data": {}}
