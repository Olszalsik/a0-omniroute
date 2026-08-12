"""
OmniRoute HTTP client used by the plugin's API handlers and execute.py.

OmniRoute exposes an OpenAI-compatible /v1/* endpoint. This client wraps
the few calls the plugin needs (health, list models, send a test
completion) with consistent error handling and config-driven timeouts.

Import path follows the Agent Zero plugin convention:
    from usr.plugins.omniroute.helpers.omniroute_client import OmniRouteClient

v1.1.0 — added autodetect_host() which tries common Docker-to-host
addresses in order, caching the result. This makes the plugin resilient
to Docker network topology changes (e.g. after Agent Zero v2.2 upgrade).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib import error as urlerr
from urllib import request as urlreq
from urllib.parse import quote as urlquote


PLUGIN_NAME = "omniroute"

# Cache for autodetected base URL (host:port, no /v1 suffix).
# Keyed by port so different port preferences don't collide.
_AUTODETECT_CACHE: Dict[int, Tuple[float, str]] = {}
_AUTODETECT_TTL = 30.0  # seconds


class OmniRouteError(Exception):
    """Raised when the OmniRoute endpoint is unreachable or returns an error."""


# --------------------------------------------------------------- tier classifier
#
# Best-effort tier classification of OmniRoute's upstream model IDs. Patterns
# are case-insensitive. The order of evaluation matters: cheap is checked
# before free because some cheap models also match free patterns.
#
# Tiers returned: "free" | "cheap" | "key" | "sub".
# Default for unmatched models is "sub" (paid) — the most conservative call.

_TIER_FREE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r":free",
        r"-free",
        r"free/",
        r"auto/best-free",
        r"auto/.*:free",
        r"oc/.*-free",
        r"tllm/.*free",
        r"veo-free/",
        r"veoaifree-web/",
        r"ddgw/",  # duckduckgo-web; usually free
    )
]
_TIER_CHEAP_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"auto/cheap",
        r"auto/.*:cheap",
        r"oc/deepseek-.*flash",
        r"oc/qwen.*-free",  # some are cheap rather than fully free
    )
]
_TIER_SUB_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"auto/claude",
        r"auto/pro",
        r"auto/best",
        r"pepper/",  # chipotle — paid
    )
]

_TIER_ORDER = {"free": 0, "cheap": 1, "key": 2, "sub": 3}


def classify_tier(model_id: str) -> str:
    """Return one of "free", "cheap", "key", or "sub" for a model id.

    The exact tier should be confirmed against the gateway's own dashboard;
    this is a heuristic used to populate the WebUI's tier badge and counts.
    """
    for pat in _TIER_CHEAP_PATTERNS:
        if pat.search(model_id):
            return "cheap"
    for pat in _TIER_FREE_PATTERNS:
        if pat.search(model_id):
            return "free"
    for pat in _TIER_SUB_PATTERNS:
        if pat.search(model_id):
            return "sub"
    return "sub"


def tier_sort_key(item: Dict[str, str]) -> Tuple[int, str]:
    """Sort key for tiered model lists (free → cheap → key → sub, alpha within)."""
    return (_TIER_ORDER.get(item.get("tier", "sub"), 9), item.get("id", ""))


def count_by_tier(model_ids: List[str]) -> Dict[str, int]:
    """Group a flat list of model ids by tier and return counts."""
    counts = {"free": 0, "cheap": 0, "key": 0, "sub": 0}
    for mid in model_ids:
        counts[classify_tier(mid)] += 1
    return counts


class OmniRouteClient:
    """Tiny stdlib-only HTTP client for OmniRoute.

    Uses urllib so we don't add a new dependency to the Agent Zero
    framework runtime. All calls are synchronous; API handlers in
    Agent Zero v2.5 must call the matching `*_async` wrappers below
    (or `asyncio.to_thread(self.method, ...)` directly) to avoid
    blocking the framework's single asyncio event loop.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 30,
        auto_detect: bool = True,
    ) -> None:
        self.api_key = (
            api_key
            if api_key is not None
            else os.environ.get("OMNIROUTE_API_KEY") or ""
        ).strip()
        self.timeout = int(os.environ.get("OMNIROUTE_TIMEOUT", timeout))
        # Resolve base_url: explicit -> env -> autodetect
        if base_url:
            self.base_url = base_url.rstrip("/")
        else:
            env_url = os.environ.get("OMNIROUTE_BASE_URL")
            if env_url:
                self.base_url = env_url.rstrip("/")
            elif auto_detect:
                self.base_url = autodetect_host(preferred_port=8080)
            else:
                self.base_url = "http://host.docker.internal:8080/v1"

    # ------------------------------------------------------------------ utils

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        url = f"{self.base_url}{path}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urlreq.Request(url, data=data, method=method, headers=self._headers())
        t0 = time.time()
        try:
            with urlreq.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                status = getattr(resp, "status", 200)
        except urlerr.HTTPError as e:
            raw = (
                e.read().decode("utf-8", errors="replace")
                if hasattr(e, "read")
                else str(e)
            )
            raise OmniRouteError(f"HTTP {e.code} from OmniRoute: {raw[:500]}") from e
        except urlerr.URLError as e:
            raise OmniRouteError(
                f"Cannot reach OmniRoute at {self.base_url}: {e.reason}"
            ) from e
        except Exception as e:  # pragma: no cover
            raise OmniRouteError(f"OmniRoute request failed: {e}") from e
        latency_ms = int((time.time() - t0) * 1000)
        try:
            return {
                "status": status,
                "latency_ms": latency_ms,
                "body": json.loads(raw) if raw else {},
            }
        except json.JSONDecodeError:
            return {"status": status, "latency_ms": latency_ms, "body": raw}

    # ----------------------------------------------------------------- health

    def health(self) -> Dict[str, Any]:
        """Cheap reachability probe. OmniRoute returns the upstream model list
        from /v1/models; we treat any successful 2xx as 'healthy'.

        The full list of model IDs is also returned under 'models' so callers
        can skip a second `GET /v1/models` after a successful health check.
        On failure, 'models' is an empty list.

        The shape is the same on success and failure (every key is present,
        even if its value is `None` or `[]` on the failure path) so callers
        can read every field without conditional checks. `execute.py` relies
        on this to print consistent log lines.
        """
        try:
            r = self._request("GET", "/models")
        except OmniRouteError as e:
            return {
                "ok": False,
                "error": str(e),
                "base_url": self.base_url,
                "latency_ms": None,
                "provider_count": 0,
                "sample_models": [],
                "models": [],
            }
        body = r.get("body") or {}
        raw = body.get("data") or body.get("models") or []
        ids: List[str] = []
        if isinstance(raw, list):
            for m in raw:
                if isinstance(m, dict) and m.get("id"):
                    ids.append(str(m["id"]))
                elif isinstance(m, str):
                    ids.append(m)
        return {
            "ok": True,
            "error": None,
            "base_url": self.base_url,
            "latency_ms": r.get("latency_ms"),
            "provider_count": len(ids),
            "sample_models": ids[:5],
            "models": ids,
        }

    # ----------------------------------------------------------------- models

    def list_models(self) -> List[str]:
        """Return the full list of model IDs exposed by OmniRoute."""
        try:
            r = self._request("GET", "/models")
        except OmniRouteError:
            return []
        body = r.get("body") or {}
        models = body.get("data") or body.get("models") or []
        out: List[str] = []
        for m in models:
            if isinstance(m, dict) and m.get("id"):
                out.append(str(m["id"]))
            elif isinstance(m, str):
                out.append(m)
        return out

    # ----------------------------------------------------------------- test

    def test_chat(self, model: str = "auto", prompt: str = "ping") -> Dict[str, Any]:
        """Send a tiny completion to verify end-to-end wiring."""
        return self._request(
            "POST",
            "/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 8,
                "stream": False,
            },
        )

    # ----------------------------------------------------------------- usage

    def usage(self) -> Dict[str, Any]:
        """Best-effort usage probe. OmniRoute may not expose a /usage endpoint
        publicly; callers should treat 404 as "not implemented" and fall back
        to per-tier model counts derived from `list_models()`.

        Returns the raw request envelope (status, latency_ms, body) so the
        caller can decide what "empty" looks like.
        """
        return self._request("GET", "/usage")

    # --------------------------------------------------------------- combos
    #
    # v2.6.6 — create / list gateway-side "combos" (the auto/* routes). The
    # combos API lives at {gateway_root}/api/combos, NOT under /v1, so these
    # methods derive the root from base_url via ``gateway_root()`` (strip the
    # trailing /v1). Used by ``api/combos.py`` to provision the
    # ``auto/utility-free`` route from the user's live free models.
    #
    # Gateway combo-API contract (verified 2026-08-10 against a live gateway):
    #   - target field is ``models`` (NOT ``targets``); each entry ``{"model": id}``
    #   - the gateway assigns its own UUID ``id``; our ``id``/``slug`` are ignored
    #   - the combo's model id in /v1/models is the slugified ``name`` (slash
    #     preserved, colons BANNED -> 400). So combo_id must be colon-free and
    #     IS the name. See ``create_combo`` for the upsert-by-name pattern.

    def _raw_request(
        self, method: str, url: str, body: Optional[dict] = None
    ) -> Dict[str, Any]:
        """Low-level urllib call against an absolute URL.

        Unlike ``_request``, this does NOT raise on non-2xx — it returns the
        status + parsed body so callers can inspect conflict responses (e.g.
        a 409 "combo already exists" on POST /api/combos) and react.
        Returns ``{"ok": bool, "status": int, "body": <json|text|None>, "error": str|None}``.
        """
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urlreq.Request(url, data=data, method=method, headers=self._headers())
        try:
            with urlreq.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                status = int(getattr(resp, "status", 200))
        except urlerr.HTTPError as e:
            raw = (
                e.read().decode("utf-8", errors="replace")
                if hasattr(e, "read")
                else str(e)
            )
            return {
                "ok": False,
                "status": int(e.code),
                "body": _maybe_json(raw),
                "error": f"HTTP {e.code}",
            }
        except urlerr.URLError as e:
            return {
                "ok": False,
                "status": 0,
                "body": None,
                "error": f"unreachable: {e.reason}",
            }
        except Exception as e:  # pragma: no cover
            return {
                "ok": False,
                "status": 0,
                "body": None,
                "error": f"request failed: {e}",
            }
        return {
            "ok": 200 <= status < 300,
            "status": status,
            "body": _maybe_json(raw),
            "error": None,
        }

    def create_combo(
        self,
        combo_id: str,
        strategy: str,
        targets: List[str],
        config: Optional[dict] = None,
    ) -> Dict[str, Any]:
        """Create or refresh a gateway combo (upsert by name).

        The OmniRoute gateway's combo API has three constraints the original
        v2.6.4 code got wrong (all discovered empirically against a live
        gateway, 2026-08-10):

        1. **The target field is ``models``, not ``targets`.** Sending
           ``targets`` silently creates a combo with an empty model list, so
           the combo never routes. The gateway enriches each
           ``{"model": "<id>"}`` entry with its own ``id``/``kind``/
           ``providerId``/``weight``.
        2. **The gateway assigns its own UUID ``id`` and ignores any ``id``/
           ``slug`` we send.** The combo's model id in ``/v1/models`` is the
           *slugified* ``name`` (slashes preserved, colons banned). So the
           selectable LiteLLM id is ``omniroute/<name>`` — the ``combo_id``
           passed here IS the name and must be colon-free (e.g.
           ``auto/utility-free``, NOT ``auto/utility:free``; the colon gets a
           400 "Name can only contain letters, numbers, spaces, -, _, /, .,
           [ and ]").
        3. **No authentication is required** to create/list/delete combos on a
           local gateway (the v2.6.4 "401 without api_key" note was stale for
           this gateway build).

        Because the gateway keys combos by its own UUID (not by our name), the
        old POST-then-PUT-by-name retry could never update an existing combo —
        ``PUT /api/combos/<name>`` 404s. The correct idempotent pattern is
        **upsert by name**: ``GET /api/combos`` → find the combo whose
        ``name`` matches → ``PUT /api/combos/<UUID>`` to refresh its targets
        in place; if none exists, ``POST /api/combos`` to create it. A
        duplicate-name POST is rejected by the gateway (no duplicate created),
        so we re-lookup once on POST failure and fall back to PUT in case a
        concurrent create raced us.

        Returns ``{"ok", "status", "body", "method": "POST"|"PUT", "error"}``.
        """
        root = gateway_root(self.base_url)
        body: Dict[str, Any] = {
            "name": combo_id,
            "strategy": strategy,
            "models": [{"model": t} for t in targets],
        }
        if config:
            body["config"] = config

        existing_uuid = self._find_combo_by_name(combo_id)
        if existing_uuid:
            put_url = f"{root}/api/combos/{urlquote(existing_uuid, safe='')}"
            r = self._raw_request("PUT", put_url, body)
            return {**r, "method": "PUT"}

        r = self._raw_request("POST", f"{root}/api/combos", body)
        if r["ok"]:
            return {**r, "method": "POST"}
        # A duplicate-name POST is rejected (no duplicate created). Re-lookup
        # in case the combo appeared between our GET and POST (a concurrent
        # refresh, or a stale local cache), and fall back to PUT-by-UUID.
        late_uuid = self._find_combo_by_name(combo_id)
        if late_uuid:
            put_url = f"{root}/api/combos/{urlquote(late_uuid, safe='')}"
            r2 = self._raw_request("PUT", put_url, body)
            return {**r2, "method": "PUT"}
        return {**r, "method": "POST"}

    def _find_combo_by_name(self, name: str) -> Optional[str]:
        """Return the UUID id of the gateway combo with the given name, or None.

        The gateway keys combos by its own UUID; we match on ``name`` (which
        is also the slug the combo is listed under in ``/v1/models``). Used by
        ``create_combo``'s upsert path. Tolerates any non-2xx / non-JSON
        response (returns None) so a flaky ``GET /api/combos`` just falls
        through to a fresh POST.
        """
        r = self.list_combos()
        if not r.get("ok"):
            return None
        body = r.get("body") or {}
        combos = body.get("combos") if isinstance(body, dict) else None
        if not isinstance(combos, list):
            return None
        for c in combos:
            if isinstance(c, dict) and c.get("name") == name:
                cid = c.get("id")
                if isinstance(cid, str) and cid.strip():
                    return cid.strip()
        return None

    def list_combos(self) -> Dict[str, Any]:
        """Best-effort ``GET {root}/api/combos``.

        Used by ``create_combo``'s upsert-by-name path and by the UI to check
        whether ``auto/utility-free`` already exists and how many targets it
        has. Tolerates any non-2xx / non-JSON response (returns
        ``{"ok": False, ...}``) — the gateway is the source of truth.
        """
        root = gateway_root(self.base_url)
        return self._raw_request("GET", f"{root}/api/combos")


# ----------------------------------------------------------------- combos utils


def _maybe_json(raw: str) -> Any:
    """Parse JSON if possible, else return the raw text (or None if empty)."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def gateway_root(base_url: str) -> str:
    """Strip the trailing ``/v1`` (and any trailing slash) from an OmniRoute
    base_url so it points at the gateway root, where ``/api/combos`` lives.

        http://host.docker.internal:8080/v1  ->  http://host.docker.internal:8080
        http://localhost:8080/               ->  http://localhost:8080

    Used by ``OmniRouteClient.create_combo`` / ``list_combos`` (v2.6.4).
    """
    u = (base_url or "").rstrip("/")
    if u.endswith("/v1"):
        u = u[:-3]
    return u.rstrip("/")


# ---------------------------------------------------------------- autodetect


def _tcp_probe(host: str, port: int, timeout: float = 1.5) -> bool:
    """Return True if a TCP connection to (host, port) succeeds within timeout."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _resolve(name: str) -> Optional[str]:
    """Resolve a hostname to an IPv4 string, or return None on failure."""
    try:
        infos = socket.getaddrinfo(name, None, socket.AF_INET)
        return infos[0][4][0] if infos else None
    except Exception:
        return None


def autodetect_host(preferred_port: int = 8080) -> str:
    """Find a reachable OmniRoute base URL on the Docker host.

    Strategy (cached for _AUTODETECT_TTL seconds per port):
      1. Try `host.docker.internal:<port>` (Docker Desktop standard)
      2. Try `172.17.0.1:<port>` (default bridge gateway)
      3. Try `gateway.docker.internal:<port>` (older Docker Desktop)
      4. Try the container's own gateway from /proc/net/route (Linux)
      5. Fall back to `http://host.docker.internal:<port>/v1` even if not
         currently reachable, so the user gets a clear error in the UI.

    The cache prevents hammering the network on every API call. The user
    can force a re-probe by restarting the Agent Zero framework.
    """
    now = time.time()
    cached = _AUTODETECT_CACHE.get(preferred_port)
    if cached and (now - cached[0]) < _AUTODETECT_TTL:
        return cached[1]

    port = preferred_port
    candidates: List[Tuple[str, int]] = []

    # Build candidate list
    for name in ("host.docker.internal", "gateway.docker.internal"):
        ip = _resolve(name)
        if ip:
            candidates.append((ip, port))

    candidates.append(("172.17.0.1", port))

    # Try to find the container's default gateway from /proc/net/route
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                parts = line.strip().split()
                if len(parts) >= 3 and parts[1] == "00000000":
                    gw_hex = parts[2]
                    if len(gw_hex) == 8:
                        gw = ".".join(
                            str(int(gw_hex[i : i + 2], 16))
                            for i in (6, 4, 2, 0)
                        )
                        if gw not in ("0.0.0.0",) and (gw, port) not in candidates:
                            candidates.append((gw, port))
    except Exception:
        pass

    for host, p in candidates:
        if _tcp_probe(host, p, timeout=1.5):
            base = f"http://{host}:{p}/v1"
            _AUTODETECT_CACHE[preferred_port] = (now, base)
            return base

    # Nothing reachable — return the best-guess default so the UI shows a
    # meaningful error pointing at the right address.
    fallback = f"http://host.docker.internal:{port}/v1"
    _AUTODETECT_CACHE[preferred_port] = (now, fallback)
    return fallback


# --------------------------------------------------------------- async wrappers
#
# v2.5 of Agent Zero runs API handlers on a single asyncio event loop. Calling
# the synchronous urllib-based methods above from `async def process(...)` would
# block the entire framework HTTP server for the full request timeout (default
# 60s). The wrappers below run the same operations via `asyncio.to_thread`,
# which dispatches them to the framework's default ThreadPoolExecutor and lets
# the event loop keep serving other requests.
#
# API handlers MUST use the `*_async` variants. The sync versions are kept
# for CLI contexts (execute.py) and the autodetect helper.

async def health_async(client: "OmniRouteClient") -> Dict[str, Any]:
    return await asyncio.to_thread(client.health)


async def list_models_async(client: "OmniRouteClient") -> List[str]:
    return await asyncio.to_thread(client.list_models)


async def test_chat_async(
    client: "OmniRouteClient", model: str = "auto", prompt: str = "ping"
) -> Dict[str, Any]:
    return await asyncio.to_thread(client.test_chat, model, prompt)


async def request_async(
    client: "OmniRouteClient",
    method: str,
    path: str,
    body: Optional[dict] = None,
) -> Any:
    return await asyncio.to_thread(client._request, method, path, body)


async def usage_async(client: "OmniRouteClient") -> Dict[str, Any]:
    return await asyncio.to_thread(client.usage)


async def create_combo_async(
    client: "OmniRouteClient",
    combo_id: str,
    strategy: str,
    targets: List[str],
    config: Optional[dict] = None,
) -> Dict[str, Any]:
    return await asyncio.to_thread(
        client.create_combo, combo_id, strategy, targets, config
    )


async def list_combos_async(client: "OmniRouteClient") -> Dict[str, Any]:
    return await asyncio.to_thread(client.list_combos)
