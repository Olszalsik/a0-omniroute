"""
OmniRoute - utility combo provisioning endpoint.

Route: POST /api/plugins/omniroute/combos

Creates (or refreshes) the ``auto/utility-free`` combo IN THE OMNIROUTE
GATEWAY — a free-only, priority-ordered route tuned for Agent Zero's
utility model (summaries, memory, JSON sub-tasks). The combo is curated
dynamically from the user's LIVE free models (fetched via ``GET /v1/models``)
so it adapts to whichever providers they have enabled, and persisted in the
gateway's DB (survives container restart). After this succeeds,
``omniroute/auto/utility-free`` appears in Agent Zero's model picker and can
be selected for the Utility slot.

Why this is a backend endpoint and not pure browser JS: the gateway's
``/api/combos`` is at the gateway root (not ``/v1``), and the curation needs
the live free model list + the plugin's tier classifier + the
``utility_combo`` curator. Doing it server-side keeps the logic in one
testable place and avoids cross-origin browser calls to the gateway. The
plugin already proxies ``/v1/models`` the same way (see ``api/models.py``).

Request body: empty, or ``{"id": "auto/utility-free"}`` (the id is fixed for
now; the field is accepted so future variants can override it without a new
endpoint).

Response shape (success):
  {
    "ok": true,
    "combo_id": "auto/utility-free",
    "selectable_as": "omniroute/auto/utility-free",
    "strategy": "priority",
    "target_count": N,
    "targets": ["groq/...", "gemini/...", ...],     # up to 20
    "free_model_count": M,                            # raw free ids seen
    "gateway_response": { "status": 200, "method": "POST"|"PUT", ... },
    "error": null
  }

Response shape (failure — gateway unreachable / no free models / gateway
rejected the combo):
  {
    "ok": false,
    "combo_id": "auto/utility-free",
    "selectable_as": "omniroute/auto/utility-free",
    "target_count": 0,
    "targets": [],
    "free_model_count": 0,
    "gateway_response": null,
    "error": "<message>"
  }

The handler is side-effect-free with respect to the plugin folder: it only
reads the live model list and POSTs one combo to the gateway. No preset,
``config.json``, or on-disk state is touched — the user picks
``omniroute/auto/utility-free`` for the Utility slot themselves.
"""

from helpers.api import ApiHandler  # type: ignore

from usr.plugins.omniroute.helpers.omniroute_client import (  # type: ignore
    OmniRouteClient,
    OmniRouteError,
    classify_tier,
    create_combo_async,
    health_async,
)
from usr.plugins.omniroute.helpers.utility_combo import (  # type: ignore
    COMBO_ID,
    UTILITY_COMBO_STRATEGY,
    curate_utility_targets,
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
        pass
    return base_url, api_key, timeout


class Combos(ApiHandler):
    async def process(self, input_data, request):
        base_url, api_key, timeout = _resolve_config()
        client = OmniRouteClient(base_url=base_url, api_key=api_key, timeout=timeout)

        # The caller may override the id in the future; today it is fixed.
        combo_id = COMBO_ID
        if isinstance(input_data, dict) and input_data.get("id"):
            combo_id = str(input_data["id"]).strip() or COMBO_ID

        selectable = f"omniroute/{combo_id}"

        # 1) Live free models from the gateway (one GET /v1/models).
        try:
            health = await health_async(client)
        except OmniRouteError as e:
            return _fail(combo_id, selectable, str(e))

        if not health.get("ok"):
            return _fail(
                combo_id, selectable, health.get("error") or "gateway not reachable"
            )

        raw_models = health.get("models") or []
        free_ids = [mid for mid in raw_models if classify_tier(mid) == "free"]

        # 2) Curate the ordered target list for the utility slot.
        targets = curate_utility_targets(free_ids)

        if not targets:
            return _fail(
                combo_id,
                selectable,
                "No free models suited for the utility slot were found. "
                "Enable at least one free provider in the OmniRoute gateway "
                "and click Re-detect, then try again.",
                free_model_count=len(free_ids),
            )

        # 3) Create / refresh the combo in the gateway.
        try:
            gw = await create_combo_async(
                client, combo_id, UTILITY_COMBO_STRATEGY, targets
            )
        except OmniRouteError as e:
            return _fail(
                combo_id, selectable, str(e), free_model_count=len(free_ids)
            )

        if not gw.get("ok"):
            return _fail(
                combo_id,
                selectable,
                _combo_create_error(gw),
                free_model_count=len(free_ids),
                gateway_response=gw,
            )

        return {
            "ok": True,
            "combo_id": combo_id,
            "selectable_as": selectable,
            "strategy": UTILITY_COMBO_STRATEGY,
            "target_count": len(targets),
            "targets": targets[:20],
            "free_model_count": len(free_ids),
            "gateway_response": {
                "status": gw.get("status"),
                "method": gw.get("method"),
                "body": gw.get("body"),
            },
            "error": None,
        }


def _fail(
    combo_id: str,
    selectable: str,
    error: str,
    free_model_count: int = 0,
    gateway_response=None,
) -> dict:
    return {
        "ok": False,
        "combo_id": combo_id,
        "selectable_as": selectable,
        "strategy": UTILITY_COMBO_STRATEGY,
        "target_count": 0,
        "targets": [],
        "free_model_count": free_model_count,
        "gateway_response": gateway_response,
        "error": error,
    }


def _combo_create_error(gw: dict) -> str:
    """Turn a failed gateway combo response into a user-actionable message.

    On a default local gateway, combo creation (``POST /api/combos``) is
    **unauthenticated** — listing free models (``GET /v1/models``) and creating
    combos both work with no API key. A **401 Authentication required** only
    appears when the gateway has been configured to require an admin token for
    its management endpoints (``OMNIROUTE_API_KEY`` set on the gateway). When
    that happens we call it out by name and point at the exact setting instead
    of burying it in a generic "gateway rejected the combo" string — which is
    what made the old v2.6.4 failure (actually a colon-in-name 400, misread as
    a 401) look like a mysterious bug rather than a fixable config issue.
    """
    status = gw.get("status")
    if status == 401:
        return (
            "Creating a combo requires the OmniRoute API key (the gateway "
            "returned 401: Authentication required). Add it in Settings -> "
            "OmniRoute -> API key (the gateway's control token, shown in the "
            "OmniRoute gateway UI at the host URL), save, then click Create / "
            "refresh again. Listing and using free models needs no key — only "
            "combo creation does."
        )
    detail = gw.get("error") or gw.get("body") or "unknown error"
    return (
        f"Gateway rejected the combo (HTTP {status}): {detail}. "
        f"You can also create it manually in the gateway's Combos dashboard."
    )