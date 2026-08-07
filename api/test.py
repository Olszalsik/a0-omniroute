"""
OmniRoute - one-shot test call endpoint.

Route: POST /api/plugins/omniroute/test

Sends a tiny completion through the configured OmniRoute instance to
verify the full chain: plugin config -> gateway -> upstream provider.
Useful from the WebUI "Test connection" button.

Input (optional):
  { "model": "auto", "prompt": "ping" }

Response (success):
  { "ok": true, "model": "...", "reply": "...", "latency_ms": N }

Response (failure):
  { "ok": false, "error": "..." }
"""

from helpers.api import ApiHandler  # type: ignore

from usr.plugins.omniroute.helpers.omniroute_client import (  # type: ignore
    OmniRouteClient,
    OmniRouteError,
    test_chat_async,
)


PLUGIN_NAME = "omniroute"


class Test(ApiHandler):
    async def process(self, input_data, request):
        base_url, api_key, timeout = _resolve_config()
        model = "auto"
        prompt = "Reply with the single word: pong"
        if isinstance(input_data, dict):
            if input_data.get("model"):
                model = str(input_data["model"]).strip() or "auto"
            if input_data.get("prompt"):
                prompt = str(input_data["prompt"]).strip() or prompt

        client = OmniRouteClient(base_url=base_url, api_key=api_key, timeout=timeout)
        try:
            r = await test_chat_async(client, model=model, prompt=prompt)
        except OmniRouteError as e:
            return {"ok": False, "error": str(e)}

        body = r.get("body") or {}
        reply = ""
        try:
            reply = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
        except Exception:
            reply = ""
        return {
            "ok": True,
            "model": model,
            "reply": reply,
            "latency_ms": r.get("latency_ms"),
            "upstream_model": (body.get("model") if isinstance(body, dict) else None),
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
