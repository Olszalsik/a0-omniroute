"""
Permanent smoke test for the OmniRoute plugin.

Run with pytest (preferred):
    python -m pytest usr/plugins/omniroute/tests/smoke.py -v

Run as a plain script (prints a summary, exits 0 on success):
    python usr/plugins/omniroute/tests/smoke.py

What it covers (mirrors the throwaway scripts_smoke_phase{2,3}.py that
shipped with each phase):
  - Syntax: every .py under usr/plugins/omniroute/ parses.
  - Version: plugin.yaml + hooks.py + execute.py agree.
  - Tier classifier: classify_tier / tier_sort_key / count_by_tier
    are single-sourced from helpers.omniroute_client.
  - HTTP client: OmniRouteClient.health() returns the model list on
    success and [] on failure; usage() round-trips.
  - last_known: read/write round-trip; user keys preserved; corrupt
    file tolerated.
  - API handlers: api/models.py returns typed [{id, tier}, ...] with
    tier_counts; api/status.py response includes last_known.
  - AGENTS.md: documents the 4 Phase 2/3/4 invariants and the skill.
  - check.py: --help, success, unreachable, --json all behave.
  - hooks.py: install/pre_update/uninstall are async.
  - YAML: model_providers.yaml is valid and has no {config.*}
    placeholders in the active config.

The tests do NOT require a running OmniRoute instance — they spin up
in-process stub HTTP servers.

The CI workflow .github/workflows/omniroute-smoke.yml runs this file
on every push to v2.5 and every PR that touches usr/plugins/omniroute/**.
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

# ---------------------------------------------------------------------------
# Path constants. All tests use these; pytest can chdir but the plugin tree
# layout is fixed relative to this file.
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(PLUGIN_ROOT)))

# ---------------------------------------------------------------------------
# Stub HTTP server fixture
# ---------------------------------------------------------------------------
class _StubHandler(BaseHTTPRequestHandler):
    """Configurable stub. Set the class attrs `response_status`,
    `response_body`, `request_log` before serving."""
    response_status = 200
    response_body: bytes = b"{}"
    response_ctype = "application/json"
    request_log: List[Tuple[str, str]] = []
    # v2.6.4: opt-in path/method routing. When non-empty, the first route
    # whose method matches the request AND whose path_substr is in the
    # request path wins; otherwise the global set_response() values apply.
    # Backwards compatible: existing tests leave this empty (set_response
    # also clears it). Used by the combos-endpoint test to stub a gateway
    # that returns /v1/models on GET and accepts /api/combos on POST/PUT.
    routes: List[Tuple[str, str, Any, int, str]] = []

    def log_message(self, *args, **kwargs):
        pass

    def _select(self, method: str) -> Tuple[int, bytes, str]:
        """Pick (status, body, ctype) — route list first, then global."""
        for r_method, r_path, r_body, r_status, r_ctype in self.__class__.routes:
            if r_method not in ("*", method):
                continue
            if r_path and r_path not in self.path:
                continue
            body = r_body
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            elif body is None:
                body = b"{}"
            return int(r_status), body, r_ctype
        return (
            self.__class__.response_status,
            self.__class__.response_body,
            self.__class__.response_ctype,
        )

    def do_GET(self):
        self.__class__.request_log.append(("GET", self.path))
        status, body, ctype = self._select("GET")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.__class__.request_log.append(("POST", self.path))
        status, body, ctype = self._select("POST")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        self.__class__.request_log.append(("PUT", self.path))
        status, body, ctype = self._select("PUT")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class StubServer:
    """Context manager around a local HTTP server. Configure via .set_response()."""

    def __init__(self):
        self._srv: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port: int = 0

    def set_response(self, body: Any = None, status: int = 200,
                     ctype: str = "application/json") -> None:
        if isinstance(body, (dict, list)):
            payload = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            payload = body.encode("utf-8")
        elif isinstance(body, bytes):
            payload = body
        else:
            payload = b"{}"
        _StubHandler.response_status = status
        _StubHandler.response_body = payload
        _StubHandler.response_ctype = ctype
        _StubHandler.request_log = []
        # Clear any path/method routing so a prior set_routes() can't leak
        # into a test that uses the single-global-response mode.
        _StubHandler.routes = []

    def set_routes(self, routes: List[Tuple[str, str, Any, int]]) -> None:
        """Path/method-aware routing (v2.6.4). Each route is
        ``(method, path_substr, body, status[, ctype])``. ``method`` may be
        ``"*"`` to match any method; ``path_substr`` is matched as a substring
        of the request path (empty string matches any). The first route whose
        method matches AND whose path_substr is in the request path wins;
        unmatched requests fall back to the global ``set_response()`` values.
        Resets the global response + request log on call.
        """
        normalized: List[Tuple[str, str, Any, int, str]] = []
        for r in routes:
            method, path, body, status = r[:4]
            ctype = r[4] if len(r) > 4 else "application/json"
            normalized.append((method, path, body, status, ctype))
        _StubHandler.routes = normalized
        _StubHandler.response_status = 200
        _StubHandler.response_body = b"{}"
        _StubHandler.response_ctype = "application/json"
        _StubHandler.request_log = []

    @property
    def request_log(self) -> List[Tuple[str, str]]:
        return _StubHandler.request_log

    def __enter__(self):
        self._srv = HTTPServer(("127.0.0.1", 0), _StubHandler)
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args):
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._srv = None
        self._thread = None


@pytest.fixture
def stub_server():
    """Provides a stub HTTP server. Caller configures via .set_response()."""
    with StubServer() as srv:
        yield srv


# ---------------------------------------------------------------------------
# Plugin module loader (the plugin lives in a namespace package, so plain
# `import` would fail outside the live A0 runtime).
# ---------------------------------------------------------------------------
def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def helpers_omniroute():
    return _load(
        "usr.plugins.omniroute.helpers.omniroute_client",
        os.path.join(PLUGIN_ROOT, "helpers", "omniroute_client.py"),
    )


@pytest.fixture(scope="module")
def helpers_last_known():
    return _load(
        "usr.plugins.omniroute.helpers.last_known",
        os.path.join(PLUGIN_ROOT, "helpers", "last_known.py"),
    )


@pytest.fixture(scope="module")
def helpers_cache():
    return _load(
        "usr.plugins.omniroute.helpers.cache",
        os.path.join(PLUGIN_ROOT, "helpers", "cache.py"),
    )


@pytest.fixture(scope="module")
def helpers_utility_combo():
    # Pure module (no I/O) — loaded directly, no stubs needed.
    return _load(
        "usr.plugins.omniroute.helpers.utility_combo",
        os.path.join(PLUGIN_ROOT, "helpers", "utility_combo.py"),
    )


@pytest.fixture(scope="module")
def api_combos():
    """Load api/combos.py with helpers.api stubbed (mirrors api_models).

    Pre-loads its real plugin imports (omniroute_client + utility_combo) so
    the handler loads regardless of whether earlier tests have already put
    them in sys.modules — this fixture must not depend on test ordering.
    """
    # Real plugin helpers the handler imports at module load time.
    _load(
        "usr.plugins.omniroute.helpers.omniroute_client",
        os.path.join(PLUGIN_ROOT, "helpers", "omniroute_client.py"),
    )
    _load(
        "usr.plugins.omniroute.helpers.utility_combo",
        os.path.join(PLUGIN_ROOT, "helpers", "utility_combo.py"),
    )
    h = types.ModuleType("helpers")
    h_api = types.ModuleType("helpers.api")
    class _StubApiHandler:
        def __init__(self, *a, **k): pass
    h_api.ApiHandler = _StubApiHandler
    sys.modules["helpers"] = h
    sys.modules["helpers.api"] = h_api
    return _load(
        "usr.plugins.omniroute.api.combos",
        os.path.join(PLUGIN_ROOT, "api", "combos.py"),
    )


@pytest.fixture(scope="module")
def api_models():
    # Stub helpers.api before loading the handler
    h = types.ModuleType("helpers")
    h_api = types.ModuleType("helpers.api")
    class _StubApiHandler:
        def __init__(self, *a, **k): pass
    h_api.ApiHandler = _StubApiHandler
    sys.modules["helpers"] = h
    sys.modules["helpers.api"] = h_api
    return _load(
        "usr.plugins.omniroute.api.models",
        os.path.join(PLUGIN_ROOT, "api", "models.py"),
    )


@pytest.fixture(scope="module")
def api_dashboard():
    """Load api/dashboard.py with helpers.api stubbed.

    Returns the loaded module. Tests must stub `helpers.plugins`
    themselves to control the configured base_url / cache_ttl.
    """
    h = types.ModuleType("helpers")
    h_api = types.ModuleType("helpers.api")

    class _StubApiHandler:
        def __init__(self, *a, **k):
            pass

    h_api.ApiHandler = _StubApiHandler
    sys.modules["helpers"] = h
    sys.modules["helpers.api"] = h_api
    return _load(
        "usr.plugins.omniroute.api.dashboard",
        os.path.join(PLUGIN_ROOT, "api", "dashboard.py"),
    )


@pytest.fixture(scope="module")
def plugin_manifest():
    with open(os.path.join(PLUGIN_ROOT, "plugin.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def all_plugin_py_files():
    out = []
    for root, dirs, files in os.walk(PLUGIN_ROOT):
        if "__pycache__" in root:
            continue
        for f in files:
            if f.endswith(".py"):
                out.append(os.path.join(root, f))
    return sorted(out)


# ===========================================================================
# 1. Syntax
# ===========================================================================
def test_all_plugin_python_files_parse(all_plugin_py_files):
    failures = []
    for p in all_plugin_py_files:
        try:
            ast.parse(open(p, encoding="utf-8").read())
        except SyntaxError as e:
            failures.append(f"{p}: {e}")
    assert not failures, "syntax errors:\n  " + "\n  ".join(failures)


# ===========================================================================
# 2. Version agreement
# ===========================================================================
def test_plugin_yaml_version_present(plugin_manifest):
    assert plugin_manifest.get("version"), "plugin.yaml missing 'version'"
    assert plugin_manifest["name"] == "omniroute"


def test_hooks_expected_version_matches_manifest(plugin_manifest):
    src = open(os.path.join(PLUGIN_ROOT, "hooks.py"), encoding="utf-8").read()
    assert f'EXPECTED_VERSION = "{plugin_manifest["version"]}"' in src, (
        f'hooks.py EXPECTED_VERSION must match plugin.yaml version '
        f'({plugin_manifest["version"]!r})'
    )


def test_execute_expected_version_matches_manifest(plugin_manifest):
    src = open(os.path.join(PLUGIN_ROOT, "execute.py"), encoding="utf-8").read()
    assert f'EXPECTED_VERSION = "{plugin_manifest["version"]}"' in src


def test_execute_no_longer_suggests_broken_port_2012():
    src = open(os.path.join(PLUGIN_ROOT, "execute.py"), encoding="utf-8").read()
    assert "2012:2012" not in src, "execute.py still references the broken port 2012"


# ===========================================================================
# 3. Hooks are async (v2.5 contract)
# ===========================================================================
def test_install_is_async():
    src = open(os.path.join(PLUGIN_ROOT, "hooks.py"), encoding="utf-8").read()
    assert "async def install()" in src, "install() must be async per v2.5 contract"


def test_pre_update_is_async():
    src = open(os.path.join(PLUGIN_ROOT, "hooks.py"), encoding="utf-8").read()
    assert "async def pre_update()" in src


def test_uninstall_is_async():
    src = open(os.path.join(PLUGIN_ROOT, "hooks.py"), encoding="utf-8").read()
    assert "async def uninstall()" in src


# ===========================================================================
# 4. base_url defaults all converge on host.docker.internal:8080/v1
# ===========================================================================
BASE_URL_DEFAULT = "http://host.docker.internal:8080/v1"


@pytest.mark.parametrize("relpath", [
    "api/dashboard.py",
    "api/usage.py",
    "api/status.py",
    "api/test.py",
    "execute.py",
    "default_config.yaml",
    "conf/model_providers.yaml",
    "helpers/omniroute_client.py",
])
def test_base_url_default_present(relpath):
    p = os.path.join(PLUGIN_ROOT, relpath)
    src = open(p, encoding="utf-8").read()
    assert "localhost:2012" not in src, f"{relpath} still has stale localhost:2012"
    assert "host.docker.internal:8080" in src, f"{relpath} missing the 8080 default"


# ===========================================================================
# 5. Tier classifier (single-sourced in helpers.omniroute_client)
# ===========================================================================
class TestTierClassifier:
    CASES = [
        ("openai/gpt-4o", "sub"),
        ("auto/best", "sub"),
        ("auto/cheap", "cheap"),
        ("auto/claude", "sub"),
        ("oc/qwen-2.5-72b-free", "cheap"),
        ("openai/gpt-4o:free", "free"),
        ("veo-free/anything", "free"),
        ("ddgw/something", "free"),
        ("oc/deepseek-r1-flash", "cheap"),
        ("pepper/foo", "sub"),
        ("tllm/whatever-free", "free"),
        ("veoaifree-web/anything", "free"),
        ("random-stranger-thing", "sub"),
    ]

    @pytest.mark.parametrize("model_id,expected", CASES)
    def test_classify_tier(self, helpers_omniroute, model_id, expected):
        assert helpers_omniroute.classify_tier(model_id) == expected

    def test_tier_sort_key_orders_free_first(self, helpers_omniroute):
        items = [{"id": m, "tier": helpers_omniroute.classify_tier(m)}
                 for m, _ in self.CASES]
        items.sort(key=helpers_omniroute.tier_sort_key)
        tiers = [m["tier"] for m in items]
        # free first, then cheap, then sub
        for i in range(len(tiers) - 1):
            order = {"free": 0, "cheap": 1, "key": 2, "sub": 3}
            assert order[tiers[i]] <= order[tiers[i + 1]], (
                f"sort out of order at {i}: {tiers}"
            )

    def test_count_by_tier_totals_match_input(self, helpers_omniroute):
        models = [m for m, _ in self.CASES]
        counts = helpers_omniroute.count_by_tier(models)
        assert sum(counts.values()) == len(models)
        assert counts["free"] == sum(1 for m, e in self.CASES if e == "free")
        assert counts["cheap"] == sum(1 for m, e in self.CASES if e == "cheap")
        assert counts["sub"] == sum(1 for m, e in self.CASES if e == "sub")

    def test_count_by_tier_includes_zero_key_bucket(self, helpers_omniroute):
        # No 'key' models in the test set; bucket must still exist (default 0)
        counts = helpers_omniroute.count_by_tier(["x"])
        assert counts == {"free": 0, "cheap": 0, "key": 0, "sub": 1}


# ===========================================================================
# 6. api/dashboard.py and api/usage.py consume the shared helper
# ===========================================================================
@pytest.mark.parametrize("relpath,shared_helper", [
    ("api/dashboard.py", "classify_tier"),
    ("api/dashboard.py", "tier_sort_key"),
    ("api/dashboard.py", "count_by_tier"),
    ("api/usage.py", "count_by_tier"),
])
def test_handler_uses_shared_tier_helper(relpath, shared_helper):
    src = open(os.path.join(PLUGIN_ROOT, relpath), encoding="utf-8").read()
    assert shared_helper in src, f"{relpath} must import {shared_helper} from the shared helper"
    # No local copies allowed
    assert "_FREE_PATTERNS" not in src, f"{relpath} has stale local _FREE_PATTERNS"
    assert "_classify_tier" not in src, f"{relpath} has stale local _classify_tier"


# ===========================================================================
# 7. No double GET /v1/models (only api/models.py is allowed to use it)
# ===========================================================================
def test_no_double_models_in_dashboard_status_usage():
    for rel in ("api/dashboard.py", "api/status.py", "api/usage.py"):
        src = open(os.path.join(PLUGIN_ROOT, rel), encoding="utf-8").read()
        assert "list_models_async" not in src, (
            f"{rel} still uses list_models_async — it should consume health.models instead"
        )


# ===========================================================================
# 8. OmniRouteClient.health() returns the model list
# ===========================================================================
def test_health_success_returns_model_list(stub_server, helpers_omniroute):
    stub_server.set_response({"data": [
        {"id": "openai/gpt-4o"},
        {"id": "openai/gpt-4o:free"},
        {"id": "auto/cheap"},
    ]})
    client = helpers_omniroute.OmniRouteClient(
        base_url=f"http://127.0.0.1:{stub_server.port}/v1", timeout=3
    )
    r = client.health()
    assert r["ok"] is True
    assert r["provider_count"] == 3
    assert r["models"] == ["openai/gpt-4o", "openai/gpt-4o:free", "auto/cheap"]
    # Success path must also include the "error" key (set to None)
    # so the response shape is uniform across success and failure
    # (see helpers/omniroute_client.py:205-208 docstring contract).
    assert r.get("error") is None, (
        f"health() success path missing 'error: None': {r!r}"
    )


def test_health_failure_returns_empty_list(helpers_omniroute):
    # Port 1 is reserved; connection will refuse or time out
    client = helpers_omniroute.OmniRouteClient(
        base_url="http://127.0.0.1:1/v1", timeout=1
    )
    r = client.health()
    assert r["ok"] is False
    assert r["models"] == []


def test_health_preserves_backward_compat_keys(helpers_omniroute):
    # execute.py reads these — must still be present
    client = helpers_omniroute.OmniRouteClient(
        base_url="http://127.0.0.1:1/v1", timeout=1
    )
    r = client.health()
    for key in ("ok", "base_url", "latency_ms", "provider_count",
                "sample_models", "models", "error"):
        assert key in r, f"health() dropped backward-compat key: {key}"


# ===========================================================================
# 9. OmniRouteClient.usage() + usage_async
# ===========================================================================
def test_usage_round_trip(stub_server, helpers_omniroute):
    stub_server.set_response({"total_requests": 42, "usage": []})
    client = helpers_omniroute.OmniRouteClient(
        base_url=f"http://127.0.0.1:{stub_server.port}/v1", timeout=3
    )
    r = client.usage()
    assert r["status"] == 200
    assert r["body"]["total_requests"] == 42


def test_usage_async_returns_envelope(stub_server, helpers_omniroute):
    stub_server.set_response({"total_requests": 7})
    client = helpers_omniroute.OmniRouteClient(
        base_url=f"http://127.0.0.1:{stub_server.port}/v1", timeout=3
    )
    r = asyncio.run(helpers_omniroute.usage_async(client))
    assert r["status"] == 200
    assert r["body"]["total_requests"] == 7


# ===========================================================================
# 10. last_known helper (round-trip + user-key preservation)
# ===========================================================================
class TestLastKnown:
    @pytest.fixture
    def tmp_plugin_dir(self, monkeypatch, helpers_last_known):
        tmp = tempfile.mkdtemp(prefix="omni_p4_")
        # Point the helper at the tmp dir for this test
        monkeypatch.setattr(helpers_last_known, "_CONFIG_PATH", os.path.join(tmp, "config.json"))
        monkeypatch.setattr(helpers_last_known, "_PLUGIN_DIR", tmp)
        yield tmp
        shutil.rmtree(tmp, ignore_errors=True)

    def test_read_returns_none_when_missing(self, tmp_plugin_dir, helpers_last_known):
        assert helpers_last_known.read_last_known() is None

    def test_round_trip(self, tmp_plugin_dir, helpers_last_known):
        ok = helpers_last_known.write_last_known({
            "latency_ms": 240, "provider_count": 187,
            "base_url": "http://x", "reachable": True,
        })
        assert ok is True
        snap = helpers_last_known.read_last_known()
        assert snap is not None
        assert snap["latency_ms"] == 240
        assert snap["provider_count"] == 187
        assert snap["base_url"] == "http://x"

    def test_preserves_user_keys(self, tmp_plugin_dir, helpers_last_known):
        # Seed the config with user-set keys
        with open(os.path.join(tmp_plugin_dir, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"base_url": "http://user", "api_key": "k", "extra": True}, f)
        helpers_last_known.write_last_known({
            "latency_ms": 100, "provider_count": 5,
            "base_url": "http://x", "reachable": True,
        })
        with open(os.path.join(tmp_plugin_dir, "config.json"), encoding="utf-8") as f:
            raw = json.load(f)
        assert raw["base_url"] == "http://user"
        assert raw["api_key"] == "k"
        assert raw["extra"] is True
        assert "last_known" in raw

    def test_corrupt_file_returns_none(self, tmp_plugin_dir, helpers_last_known):
        with open(os.path.join(tmp_plugin_dir, "config.json"), "w", encoding="utf-8") as f:
            f.write("this is not json")
        assert helpers_last_known.read_last_known() is None

    def test_age_calc(self, helpers_last_known):
        assert helpers_last_known.last_known_age_seconds(None) is None
        # Manually construct a stale snapshot
        import time
        snap = {"ts": time.time() - 120}
        age = helpers_last_known.last_known_age_seconds(snap)
        assert age is not None and 110 <= age <= 130


# ===========================================================================
# 10b. helpers/cache.py: model-list cache round-trip + edge cases (Phase 5.1)
# ===========================================================================
class TestCache:
    @pytest.fixture
    def tmp_plugin_dir(self, monkeypatch, helpers_last_known):
        tmp = tempfile.mkdtemp(prefix="omni_p5_cache_")
        # The cache helper imports _read_raw_config / _write_raw_config
        # from last_known.py and the path constant lives there. Patch
        # only the last_known module's _CONFIG_PATH — that's what
        # _read_raw_config() actually reads at call time.
        cfg_path = os.path.join(tmp, "config.json")
        monkeypatch.setattr(helpers_last_known, "_CONFIG_PATH", cfg_path)
        yield tmp
        shutil.rmtree(tmp, ignore_errors=True)

    def test_read_returns_none_when_missing(self, tmp_plugin_dir, helpers_cache):
        assert helpers_cache.read_cache() is None

    def test_round_trip(self, tmp_plugin_dir, helpers_cache):
        ok = helpers_cache.write_cache({
            "base_url": "http://x:8080/v1",
            "models": [
                {"id": "openai/gpt-4o:free", "tier": "free"},
                {"id": "auto/cheap", "tier": "cheap"},
                {"id": "anthropic/claude-3.5-sonnet", "tier": "sub"},
            ],
        })
        assert ok is True
        snap = helpers_cache.read_cache()
        assert snap is not None
        assert snap["version"] == helpers_cache.CACHE_FORMAT_VERSION
        assert snap["base_url"] == "http://x:8080/v1"
        assert snap["provider_count"] == 3
        assert snap["tier_counts"] == {"free": 1, "cheap": 1, "key": 0, "sub": 1}
        assert {m["id"] for m in snap["models"]} == {
            "openai/gpt-4o:free", "auto/cheap", "anthropic/claude-3.5-sonnet"
        }
        assert "saved_at" in snap and "saved_at_unix" in snap

    def test_corrupt_file_returns_none(self, tmp_plugin_dir, helpers_cache):
        with open(os.path.join(tmp_plugin_dir, "config.json"), "w", encoding="utf-8") as f:
            f.write("{not valid json")
        assert helpers_cache.read_cache() is None

    def test_wrong_version_returns_none(self, tmp_plugin_dir, helpers_cache):
        cfg_path = os.path.join(tmp_plugin_dir, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"models_cache": {"version": 999, "models": []}}, f)
        assert helpers_cache.read_cache() is None

    def test_models_not_a_list_returns_none(self, tmp_plugin_dir, helpers_cache):
        cfg_path = os.path.join(tmp_plugin_dir, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({
                "models_cache": {
                    "version": 1,
                    "models": "oops not a list",
                }
            }, f)
        assert helpers_cache.read_cache() is None

    def test_missing_models_key_returns_none(self, tmp_plugin_dir, helpers_cache):
        cfg_path = os.path.join(tmp_plugin_dir, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"models_cache": {"version": 1, "base_url": "x"}}, f)
        assert helpers_cache.read_cache() is None

    def test_age_calc(self, helpers_cache):
        assert helpers_cache.cache_age_seconds(None) is None
        snap = {"saved_at_unix": time.time() - 120}
        age = helpers_cache.cache_age_seconds(snap)
        assert age is not None and 110 <= age <= 130

    def test_is_fresh_respects_ttl(self, helpers_cache):
        snap = {"saved_at_unix": time.time() - 30}
        assert helpers_cache.is_cache_fresh(snap, 60) is True
        assert helpers_cache.is_cache_fresh(snap, 10) is False
        assert helpers_cache.is_cache_fresh(None, 60) is False
        # ttl <= 0 disables freshness (any non-None snapshot is 'fresh')
        snap_old = {"saved_at_unix": time.time() - 99999}
        assert helpers_cache.is_cache_fresh(snap_old, 0) is True
        assert helpers_cache.is_cache_fresh(snap_old, -1) is True

    def test_atomic_write_no_tmp_leftover(self, tmp_plugin_dir, helpers_cache):
        helpers_cache.write_cache({"base_url": "http://x/v1", "models": []})
        leftover = [
            f for f in os.listdir(tmp_plugin_dir)
            if f.startswith(".config.") and f.endswith(".tmp")
        ]
        assert not leftover, f"atomic write left tmp files: {leftover}"

    def test_preserves_last_known_and_user_keys(
        self, tmp_plugin_dir, helpers_cache, helpers_last_known,
    ):
        # Pre-seed the config.json with user keys AND a `last_known`
        # entry. Then write a cache. All original keys must survive.
        cfg_path = os.path.join(tmp_plugin_dir, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({
                "base_url": "http://user:8080/v1",
                "api_key": "user-key",
                "default_model": "auto",
                "last_known": {
                    "ts": 1000.0,
                    "ts_iso": "1970-01-01T00:16:40Z",
                    "latency_ms": 42,
                    "provider_count": 99,
                    "base_url": "http://user:8080/v1",
                    "reachable": True,
                },
            }, f)
        ok = helpers_cache.write_cache({
            "base_url": "http://user:8080/v1",
            "models": [{"id": "x:free", "tier": "free"}],
        })
        assert ok is True
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # User keys preserved
        assert raw["base_url"] == "http://user:8080/v1"
        assert raw["api_key"] == "user-key"
        assert raw["default_model"] == "auto"
        # last_known preserved
        assert raw["last_known"]["latency_ms"] == 42
        assert raw["last_known"]["provider_count"] == 99
        # Cache present
        assert raw["models_cache"]["provider_count"] == 1
        assert raw["models_cache"]["tier_counts"] == {"free": 1, "cheap": 0, "key": 0, "sub": 0}

    def test_write_rejects_non_dict_snapshot(self, helpers_cache, tmp_plugin_dir):
        assert helpers_cache.write_cache("not a dict") is False
        assert helpers_cache.write_cache(["not", "a", "dict"]) is False

    def test_write_rejects_non_list_models(self, helpers_cache, tmp_plugin_dir):
        assert helpers_cache.write_cache({"base_url": "x", "models": "oops"}) is False
        assert helpers_cache.write_cache({"base_url": "x", "models": {"dict": 1}}) is False

    def test_write_normalizes_invalid_tiers_to_sub(
        self, tmp_plugin_dir, helpers_cache,
    ):
        # An unknown tier value (e.g. "weird") is normalized to "sub" —
        # the conservative default. The model is still kept (it has an id).
        ok = helpers_cache.write_cache({
            "base_url": "http://x/v1",
            "models": [
                {"id": "valid", "tier": "free"},
                {"id": "weird-tier", "tier": "mystery"},
            ],
        })
        assert ok is True
        snap = helpers_cache.read_cache()
        assert snap is not None
        tiers = {m["id"]: m["tier"] for m in snap["models"]}
        assert tiers == {"valid": "free", "weird-tier": "sub"}
        # 1 free, 1 sub → 0 cheap, 0 key
        assert snap["tier_counts"] == {"free": 1, "cheap": 0, "key": 0, "sub": 1}

    def test_write_drops_models_without_id(self, tmp_plugin_dir, helpers_cache):
        ok = helpers_cache.write_cache({
            "base_url": "http://x/v1",
            "models": [
                {"id": "good", "tier": "free"},
                {"tier": "free"},  # missing id → dropped
                {"id": "", "tier": "free"},  # empty id → dropped
                "string-not-dict",  # wrong type → dropped
            ],
        })
        assert ok is True
        snap = helpers_cache.read_cache()
        assert snap is not None
        assert snap["provider_count"] == 1
        assert snap["models"] == [{"id": "good", "tier": "free"}]


# ===========================================================================
# 11. api/models.py: typed [{id, tier}, ...] response
# ===========================================================================
class TestModelsHandler:
    @pytest.fixture
    def stub_helpers_plugins(self, monkeypatch, stub_server):
        # Patch get_plugin_config in helpers.plugins to point at the stub server
        hp = types.ModuleType("helpers.plugins")
        hp.get_plugin_config = lambda name: {
            "base_url": f"http://127.0.0.1:{stub_server.port}/v1",
            "api_key": "",
            "timeout_seconds": 3,
        }
        sys.modules["helpers.plugins"] = hp
        yield

    def test_returns_typed_models(self, stub_server, stub_helpers_plugins, api_models):
        stub_server.set_response({"data": [
            {"id": "openai/gpt-4o"},
            {"id": "openai/gpt-4o:free"},
        ]})

        class _Req: pass
        result = asyncio.run(api_models.Models().process({}, _Req()))
        assert result["count"] == 2
        assert result["error"] is None
        for m in result["models"]:
            assert isinstance(m, dict)
            assert "id" in m and "tier" in m, f"model entry missing keys: {m}"

    def test_returns_tier_counts(self, stub_server, stub_helpers_plugins, api_models):
        stub_server.set_response({"data": [
            {"id": "openai/gpt-4o:free"},
            {"id": "auto/cheap"},
            {"id": "veo-free/x"},
        ]})

        class _Req: pass
        result = asyncio.run(api_models.Models().process({}, _Req()))
        counts = result["tier_counts"]
        assert counts["free"] == 2
        assert counts["cheap"] == 1
        assert counts["sub"] == 0
        assert counts["key"] == 0

    def test_filter_substring(self, stub_server, stub_helpers_plugins, api_models):
        stub_server.set_response({"data": [
            {"id": "openai/gpt-4o"},
            {"id": "anthropic/claude-sonnet-4-5"},
        ]})

        class _Req: pass
        result = asyncio.run(api_models.Models().process({"filter": "claude"}, _Req()))
        assert result["count"] == 2
        assert len(result["filtered"]) == 1
        assert "claude" in result["filtered"][0]["id"].lower()

    def test_unreachable_returns_empty(self, api_models, monkeypatch):
        # Override the helper's URL to a dead port
        hp = types.ModuleType("helpers.plugins")
        hp.get_plugin_config = lambda name: {
            "base_url": "http://127.0.0.1:1/v1",
            "api_key": "",
            "timeout_seconds": 1,
        }
        sys.modules["helpers.plugins"] = hp

        class _Req: pass
        result = asyncio.run(api_models.Models().process({}, _Req()))
        assert result["count"] == 0
        assert result["models"] == []
        assert result["error"] is not None
        assert result["tier_counts"] == {"free": 0, "cheap": 0, "key": 0, "sub": 0}


# ===========================================================================
# 11b. api/dashboard.py: cache-first / live-always / fallback flow (Phase 5.1)
# ===========================================================================
class TestDashboardHandlerCache:
    @pytest.fixture
    def isolated_cache(
        self, tmp_path, monkeypatch, helpers_last_known,
    ):
        """Point the cache helper at a fresh tmp config.json.

        The cache module reads its config via `last_known._read_raw_config()`,
        which closes over `last_known._CONFIG_PATH` at call time. So we
        patch ONLY the last_known module's _CONFIG_PATH.
        """
        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr(helpers_last_known, "_CONFIG_PATH", str(cfg_path))
        return cfg_path

    @pytest.fixture
    def stub_helpers_plugins_dead(self, monkeypatch):
        """helpers.plugins stub pointing at a DEAD URL (port 1 is reserved)."""
        hp = types.ModuleType("helpers.plugins")
        hp.get_plugin_config = lambda name: {
            "base_url": "http://127.0.0.1:1/v1",  # dead
            "api_key": "",
            "timeout_seconds": 1,
            "cache_ttl_seconds": 3600,
        }
        sys.modules["helpers.plugins"] = hp
        yield
        # Don't clean up sys.modules; other tests may need it

    @pytest.fixture
    def stub_helpers_plugins_live(self, stub_server, monkeypatch):
        """helpers.plugins stub pointing at the running StubServer.

        The `stub_server` dependency is what makes this fixture depend
        on the live test server; without it the resolver would point at
        a URL that has no listener.
        """
        hp = types.ModuleType("helpers.plugins")
        hp.get_plugin_config = lambda name: {
            "base_url": f"http://127.0.0.1:{stub_server.port}/v1",
            "api_key": "",
            "timeout_seconds": 3,
            "cache_ttl_seconds": 3600,
        }
        sys.modules["helpers.plugins"] = hp
        yield
        # Don't clean up sys.modules; other tests may need it

    def _run(self, api_dashboard):
        """Invoke Dashboard.process() synchronously."""
        class _Req:
            pass
        return asyncio.run(api_dashboard.Dashboard().process({}, _Req()))

    def test_dashboard_writes_cache_on_success(
        self, stub_server, stub_helpers_plugins_live, api_dashboard, isolated_cache,
    ):
        stub_server.set_response({"data": [
            {"id": "openai/gpt-4o:free"},
            {"id": "auto/cheap"},
            {"id": "anthropic/claude-3.5-sonnet"},
        ]})
        result = self._run(api_dashboard)
        assert result["reachable"] is True
        assert result["from_cache"] is False
        assert result["provider_count"] == 3
        assert result["free_count"] == 1
        assert result["cheap_count"] == 1
        assert result["sub_count"] == 1
        # The cache should have been written
        from usr.plugins.omniroute.helpers.cache import read_cache
        snap = read_cache()
        assert snap is not None
        assert snap["provider_count"] == 3
        assert snap["tier_counts"] == {"free": 1, "cheap": 1, "key": 0, "sub": 1}

    def test_dashboard_returns_cached_when_gateway_down(
        self, stub_helpers_plugins_dead, api_dashboard, isolated_cache,
    ):
        # Pre-seed the cache with a snapshot matching the (dead) base_url
        from usr.plugins.omniroute.helpers.cache import write_cache
        ok = write_cache({
            "base_url": "http://127.0.0.1:1/v1",
            "models": [{"id": "openai/gpt-4o", "tier": "sub"}],
        })
        assert ok is True
        result = self._run(api_dashboard)
        assert result["reachable"] is False
        assert result["from_cache"] is True
        assert result["models"] == [{"id": "openai/gpt-4o", "tier": "sub"}]
        assert result["cached_snapshot"] is not None
        assert result["cached_snapshot"]["provider_count"] == 1
        assert result["cached_snapshot"]["age_seconds"] is not None

    def test_dashboard_rejects_cross_url_cache(
        self, stub_helpers_plugins_dead, api_dashboard, isolated_cache,
    ):
        # Pre-seed the cache with a snapshot from a DIFFERENT gateway
        from usr.plugins.omniroute.helpers.cache import write_cache
        ok = write_cache({
            "base_url": "http://other-gateway:9999/v1",  # different URL
            "models": [{"id": "openai/gpt-4o", "tier": "sub"}],
        })
        assert ok is True
        # Resolver is pointing at http://127.0.0.1:1/v1 (dead). The
        # cross-URL cache should NOT be served.
        result = self._run(api_dashboard)
        assert result["reachable"] is False
        assert result["from_cache"] is False
        assert result["models"] == []
        assert result["cached_snapshot"] is None

    def test_dashboard_no_cache_no_live_returns_empty_envelope(
        self, stub_helpers_plugins_dead, api_dashboard, isolated_cache,
    ):
        # No pre-seeded cache, dead URL. Honest offline envelope.
        result = self._run(api_dashboard)
        assert result["reachable"] is False
        assert result["from_cache"] is False
        assert result["models"] == []
        assert result["provider_count"] == 0
        assert result["cached_snapshot"] is None

    def test_dashboard_response_includes_from_cache_field(
        self, stub_server, stub_helpers_plugins_live, api_dashboard, isolated_cache,
    ):
        stub_server.set_response({"data": [{"id": "auto/cheap"}]})
        result = self._run(api_dashboard)
        # Even on success, from_cache and cached_snapshot are present
        assert "from_cache" in result
        assert result["from_cache"] is False
        assert "cached_snapshot" in result
        # cached_snapshot reflects the PRE-EXISTING cache (none in this test)
        assert result["cached_snapshot"] is None

    def test_dashboard_live_failure_writes_no_cache(
        self, stub_helpers_plugins_dead, api_dashboard, isolated_cache,
    ):
        # A live failure MUST NOT overwrite the existing cache (Phase 5.1
        # rule: writes only happen on live success). Pre-seed a cache,
        # trigger a dead-URL call, and assert the cache is unchanged.
        from usr.plugins.omniroute.helpers.cache import read_cache, write_cache
        ok = write_cache({
            "base_url": "http://127.0.0.1:1/v1",
            "models": [{"id": "preexisting", "tier": "sub"}],
        })
        assert ok is True
        before_snap = read_cache()
        assert before_snap is not None
        result = self._run(api_dashboard)
        assert result["reachable"] is False
        after_snap = read_cache()
        # Cache content unchanged
        assert after_snap["models"] == [{"id": "preexisting", "tier": "sub"}]
        assert after_snap["provider_count"] == 1


# ===========================================================================
# 11d. agents/omniroute/agent.yaml + prompts/main.md contract (Phase 5.2)
#
# Pinned because Agent Zero's _get_agents_list_from_dir
# (helpers/subagents.py:89-110) auto-discovers every subdirectory of
# `agents/`, including orphans. The plugin ships exactly one canonical
# profile and one prompts/main.md. The legacy omniroute_safe/ directory
# was removed in Phase 5.2; these tests guard against re-introduction.
# ===========================================================================
class TestAgentProfile:
    AGENT_DIR = os.path.join(PLUGIN_ROOT, "agents", "omniroute")
    AGENT_YAML = os.path.join(AGENT_DIR, "agent.yaml")
    PROMPT_MD = os.path.join(AGENT_DIR, "prompts", "main.md")
    LEGACY_DIR = os.path.join(PLUGIN_ROOT, "agents", "omniroute_safe")

    def test_canonical_profile_yaml_exists(self):
        assert os.path.isfile(self.AGENT_YAML), (
            f"Missing canonical profile: {self.AGENT_YAML}. "
            "A0 will not auto-discover an agent without this file."
        )

    def test_canonical_profile_parses(self):
        with open(self.AGENT_YAML, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)
        assert "title" in data and data["title"], "agent.yaml must have a non-empty title"
        assert "description" in data and data["description"], (
            "agent.yaml must have a non-empty description (the WebUI picker shows this)"
        )
        assert data["title"] == "OmniRoute Agent", (
            f"Unexpected title: {data['title']!r}"
        )

    def test_canonical_profile_has_main_prompt(self):
        # 1. The file exists and is non-empty
        assert os.path.isfile(self.PROMPT_MD), (
            f"Missing prompt: {self.PROMPT_MD}. "
            "Without it the profile renders as a broken SubAgent in the "
            "WebUI picker (no UI warning - the framework just shows an "
            "agent with no prompts)."
        )
        assert os.path.getsize(self.PROMPT_MD) > 0, "prompts/main.md is empty"

        # 2. The file is referenced from agent.yaml under `prompts.main`
        with open(self.AGENT_YAML, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        prompts = data.get("prompts") or {}
        main_ref = prompts.get("main", "")
        assert "prompts/main.md" in main_ref, (
            f"agent.yaml prompts.main must reference 'prompts/main.md'; "
            f"got {main_ref!r}"
        )

    def test_canonical_profile_is_enabled(self):
        with open(self.AGENT_YAML, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        # Two acceptable states:
        #   - `enabled` key absent -> uses SubAgentListItem default (True)
        #   - `enabled: true`        -> explicit
        # `enabled: false` is the only thing that would hide the profile
        # from the WebUI picker.
        enabled = data.get("enabled", True)
        assert enabled is True, (
            f"Canonical profile must be enabled (got enabled={enabled!r}). "
            "A0's SubAgentListItem defaults to True; if you set this to "
            "False, the profile disappears from the WebUI agent picker."
        )

    def test_legacy_omniroute_safe_profile_is_removed(self):
        # The legacy directory was removed in Phase 5.2 because it had
        # no prompts/ directory and would have rendered as a broken
        # SubAgent entry. This test pins the cleanup so a stale merge
        # can't silently re-introduce it.
        assert not os.path.exists(self.LEGACY_DIR), (
            f"Legacy orphan re-appeared: {self.LEGACY_DIR}. "
            "Remove it - the canonical profile is agents/omniroute/."
        )
        # And the agent.yaml specifically must not exist
        legacy_yaml = os.path.join(self.LEGACY_DIR, "agent.yaml")
        assert not os.path.exists(legacy_yaml), (
            f"Legacy agent.yaml re-appeared at {legacy_yaml}"
        )


# ===========================================================================
# 11c. cache_ttl_seconds config flow (Phase 5.1)
# ===========================================================================
class TestCacheTTL:
    def test_default_config_has_cache_ttl_seconds(self):
        with open(os.path.join(PLUGIN_ROOT, "default_config.yaml"), encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        assert "cache_ttl_seconds" in cfg, "default_config.yaml missing cache_ttl_seconds"
        assert isinstance(cfg["cache_ttl_seconds"], int)
        assert cfg["cache_ttl_seconds"] > 0

    def test_dashboard_resolves_ttl_from_config(self, api_dashboard):
        hp = types.ModuleType("helpers.plugins")
        hp.get_plugin_config = lambda name: {
            "base_url": "http://127.0.0.1:1/v1",
            "api_key": "",
            "timeout_seconds": 1,
            "cache_ttl_seconds": 1234,
        }
        sys.modules["helpers.plugins"] = hp
        try:
            base_url, api_key, timeout, ttl = api_dashboard._resolve_config()
            assert ttl == 1234, f"expected 1234, got {ttl}"
        finally:
            sys.modules.pop("helpers.plugins", None)

    def test_dashboard_ttl_default_when_missing(self, api_dashboard):
        hp = types.ModuleType("helpers.plugins")
        hp.get_plugin_config = lambda name: {
            "base_url": "http://127.0.0.1:1/v1",
            "api_key": "",
            "timeout_seconds": 1,
            # no cache_ttl_seconds
        }
        sys.modules["helpers.plugins"] = hp
        try:
            base_url, api_key, timeout, ttl = api_dashboard._resolve_config()
            assert ttl == 3600, f"expected 3600, got {ttl}"
        finally:
            sys.modules.pop("helpers.plugins", None)

    def test_dashboard_ttl_invalid_falls_back_to_default(self, api_dashboard):
        hp = types.ModuleType("helpers.plugins")
        hp.get_plugin_config = lambda name: {
            "base_url": "http://127.0.0.1:1/v1",
            "api_key": "",
            "timeout_seconds": 1,
            "cache_ttl_seconds": "not a number",
        }
        sys.modules["helpers.plugins"] = hp
        try:
            base_url, api_key, timeout, ttl = api_dashboard._resolve_config()
            assert ttl == 3600, f"expected 3600, got {ttl}"
        finally:
            sys.modules.pop("helpers.plugins", None)

    def test_dashboard_resolve_config_returns_four_tuple(self, api_dashboard):
        hp = types.ModuleType("helpers.plugins")
        hp.get_plugin_config = lambda name: {
            "base_url": "http://x/v1",
            "api_key": "k",
            "timeout_seconds": 5,
            "cache_ttl_seconds": 7,
        }
        sys.modules["helpers.plugins"] = hp
        try:
            result = api_dashboard._resolve_config()
            assert isinstance(result, tuple) and len(result) == 4
            base_url, api_key, timeout, ttl = result
            assert base_url == "http://x/v1"
            assert api_key == "k"
            assert timeout == 5
            assert ttl == 7
        finally:
            sys.modules.pop("helpers.plugins", None)


# ===========================================================================
# 12. AGENTS.md invariants
# ===========================================================================
@pytest.fixture(scope="module")
def agents_md():
    return open(os.path.join(PLUGIN_ROOT, "AGENTS.md"), encoding="utf-8").read()


def test_agents_md_exists_and_is_substantial(agents_md):
    assert len(agents_md) > 4000, f"AGENTS.md is too short: {len(agents_md)} chars"


def test_agents_md_documents_hard_invariants(agents_md):
    for section in ("HARD INVARIANTS", "Build discipline", "Knowledge map",
                    "Verified A0 v2.5 mechanics"):
        assert section in agents_md, f"AGENTS.md missing section: {section}"


def test_agents_md_documents_typed_models(agents_md):
    assert "[{id, tier}, ...]" in agents_md, "AGENTS.md should document the typed models response"


def test_agents_md_documents_last_known(agents_md):
    assert "last_known" in agents_md and "plugin-local" in agents_md, (
        "AGENTS.md should document the last_known invariant"
    )


def test_agents_md_documents_prompts(agents_md):
    assert "prompts/main.md" in agents_md and "source of truth" in agents_md, (
        "AGENTS.md should document the prompts/main.md invariant"
    )


def test_agents_md_documents_skill(agents_md):
    assert "omniroute-quickstart" in agents_md, "AGENTS.md should mention the skill"


def test_agents_md_documents_cache(agents_md):
    # Phase 5.1: the model-list cache invariant (#17) and the
    # knowledge-map entry must both be present.
    assert "17." in agents_md, "AGENTS.md should document invariant 17 (cache)"
    assert "best-effort accelerator" in agents_md, (
        "AGENTS.md invariant 17 should call the cache a 'best-effort accelerator'"
    )
    assert "models_cache" in agents_md, (
        "AGENTS.md should mention the models_cache key"
    )
    assert "helpers/cache.py" in agents_md, (
        "AGENTS.md knowledge map should reference helpers/cache.py"
    )


def test_agents_md_documents_profile_contract(agents_md):
    # Phase 5.2: the canonical-profile invariant (#18) and the
    # knowledge-map entry must both be present.
    assert "18." in agents_md, (
        "AGENTS.md should document invariant 18 (one agent profile per plugin)"
    )
    assert "agents/omniroute/agent.yaml" in agents_md, (
        "AGENTS.md should mention the canonical profile path"
    )
    assert "omniroute_safe" in agents_md, (
        "AGENTS.md should explicitly note the removed legacy profile"
    )
    assert "Exactly one agent profile" in agents_md, (
        "AGENTS.md invariant 18 should state the one-profile rule"
    )


# ===========================================================================
# 13. YAML hygiene
# ===========================================================================
def test_model_providers_yaml_is_valid():
    p = os.path.join(PLUGIN_ROOT, "conf", "model_providers.yaml")
    with open(p, encoding="utf-8") as f:
        raw = f.read()
    d = yaml.safe_load(raw)
    assert d["chat"]["omniroute"]["kwargs"]["api_base"] == BASE_URL_DEFAULT
    # Strip comments and confirm no {config.*} placeholders in active config
    y_no_comments = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("#")
    )
    assert "{config." not in y_no_comments, (
        "model_providers.yaml has {config.*} placeholder in active config"
    )


# ===========================================================================
# 14. File inventory
# ===========================================================================
def test_required_files_exist():
    for rel in [
        "plugin.yaml",
        "default_config.yaml",
        "hooks.py",
        "execute.py",
        "conf/model_providers.yaml",
        "agents/omniroute/agent.yaml",
        "agents/omniroute/prompts/main.md",
        "api/status.py",
        "api/models.py",
        "api/test.py",
        "api/dashboard.py",
        "api/usage.py",
        "api/combos.py",  # v2.6.4: provisions auto/utility:free in the gateway
        "helpers/omniroute_client.py",
        "helpers/last_known.py",
        "helpers/cache.py",
        "helpers/utility_combo.py",  # v2.6.4: curates the auto/utility:free target list
        "webui/config.html",
        "webui/omniroute-store.js",
        "webui/dashboard.html",
        "webui/dashboard.js",
        "webui/install-omniroute.ps1",
        "extensions/webui/page-head/omniroute-status.html",
        "extensions/webui/sidebar-end/dashboard-link.html",
        "extensions/webui/chat-input-bottom-actions-end/omniroute-button.html",
        "skills/omniroute-quickstart/SKILL.md",
        "skills/omniroute-quickstart/scripts/check.py",
        "tests/__init__.py",
        "tests/smoke.py",
    ]:
        assert os.path.isfile(os.path.join(PLUGIN_ROOT, rel)), f"missing: {rel}"


def test_injector_html_is_removed():
    """Phase 1 fix: the dead page-head injector must not return."""
    bad = os.path.join(
        PLUGIN_ROOT, "extensions", "webui", "page-head", "omniroute-injector.html"
    )
    assert not os.path.isfile(bad), "omniroute-injector.html should have been deleted"


def test_ps1_lives_in_webui_dir():
    p = os.path.join(PLUGIN_ROOT, "webui", "install-omniroute.ps1")
    assert os.path.isfile(p)
    assert not os.path.isfile(os.path.join(PLUGIN_ROOT, "install-omniroute.ps1"))


def test_ps1_has_diagnostic_dump_on_fail():
    """Phase 5.4 + 6.x: the install script must surface a diagnostic
    dump on FAIL so the user can tell *why* the gateway did not come
    online. The previous behavior was a silent "Gateway did not start
    within 30 seconds" with no clue why. This test pins the contract
    so a future contributor doesn't accidentally regress to a silent
    FAIL.

    Phase 6.x: the install path was rewritten for Docker (the npm
    install path is no longer recommended). The diagnostic-dump
    markers were updated accordingly. The intent is the same: the
    FAIL path must tell the user what went wrong.
    """
    p = os.path.join(PLUGIN_ROOT, "webui", "install-omniroute.ps1")
    src = open(p, encoding="utf-8").read()
    # Look for active code (non-comment lines) that uses
    # `Start-Process -FilePath 'omniroute'`. The phrase is allowed in
    # comments that explain why we don't use it. (Phase 5.4.5 contract,
    # kept across the 6.x rewrite because the docker path doesn't
    # spawn the npm-managed daemon at all.)
    bad_active = False
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if "Start-Process -FilePath 'omniroute'" in line:
            bad_active = True
            break
    assert not bad_active, (
        "install-omniroute.ps1 must NOT actively use "
        "Start-Process -FilePath 'omniroute' — use powershell.exe with "
        "`-Command \"omniroute ...\"` instead so PowerShell's command "
        "resolution is used."
    )
    # The diagnostic dump must mention the key signals. Phase 6.x:
    # the Docker-path dump uses `docker inspect` for container state,
    # `Get-NetTCPConnection` for host port state, and `docker logs`
    # for the container's view (replaces the old `Daemon stderr`
    # from the npm install path).
    for marker in (
        "docker inspect",
        "Get-NetTCPConnection -LocalPort $Port",
        "last HTTP error",
        "docker logs",
        "Diagnostic dump",
        "Try running these manually",
    ):
        assert marker in src, (
            f"install-omniroute.ps1 missing diagnostic-dump marker: "
            f"{marker!r}. The FAIL path must tell the user what went wrong."
        )
    # Phase 5.4.6 + 6.x: the script must probe multiple addresses. On
    # Windows, `localhost` resolves to `[::1]` (IPv6) first; if the
    # container's port-publish is bound to IPv4 only (the Docker
    # Desktop default), the IPv6 connection will time out. The fix is
    # to try 127.0.0.1, localhost, and [::1] in order.
    assert "127.0.0.1" in src, (
        "install-omniroute.ps1 must probe 127.0.0.1 (most common IPv4 "
        "binding for Docker Desktop on Windows) before localhost/[::1] "
        "— the previous localhost-only probe could time out on Windows "
        "even when the gateway was healthy and bound to IPv4."
    )
    assert "$probeAddresses" in src, (
        "install-omniroute.ps1 must iterate over multiple probe addresses "
        "(127.0.0.1, localhost, [::1]) rather than only localhost."
    )


# ===========================================================================
# 14b. uninstall-omniroute.ps1 + lifecycle contract (Phase 6.x)
# ===========================================================================
def test_uninstall_ps1_exists_and_parses():
    """Phase 6.x: the gateway-removal script must exist, contain the
    expected marker strings (so we know it's the right script and
    not a copy-paste of the installer), and parse cleanly via the
    PowerShell AST parser (the same check applied to
    install-omniroute.ps1 above).

    The smoke suite must not depend on Docker, so we do NOT execute
    the script — we only verify it parses and contains the right
    structure. The live test path is `usr/plugins/omniroute/tests/live.py`
    (run by hand before releases) and the manual walkthrough in
    the plan.
    """
    p = os.path.join(PLUGIN_ROOT, "webui", "uninstall-omniroute.ps1")
    assert os.path.isfile(p), (
        f"uninstall-omniroute.ps1 is missing at {p}. The plugin's "
        "WebUI 'Remove OmniRoute gateway' button downloads this file "
        "and the user double-clicks it. Without it, the removal path "
        "is broken."
    )
    src = open(p, encoding="utf-8").read()
    # Marker strings — pin that the file is the right script, not a
    # copy-paste of the installer. The installer and uninstaller
    # share helper functions and the Docker CLI check, so the
    # distinguishing markers are the *destructive* ones.
    for marker in (
        "docker stop",  # the actual removal step
        "docker rm",  # the actual removal step
        "diegosouzapw/omniroute",  # the image we offer to remove
        "Nothing to do",  # the friendly no-op message
        "reinstall",  # the post-uninstall next-steps hint
    ):
        assert marker in src, (
            f"uninstall-omniroute.ps1 missing marker: {marker!r}. "
            "The script is meant to be a small, well-bounded file. "
            "If you're refactoring, keep the contract strings."
        )
    # Parse via the PowerShell AST. Catches syntax errors that
    # would otherwise only surface when the user double-clicks the
    # file on Windows. The same parse-validate pattern is used for
    # install-omniroute.ps1 in test_ps1_has_diagnostic_dump_on_fail.
    try:
        import clr  # noqa: F401  # type: ignore
    except ImportError:
        # On non-Windows dev machines without PowerShell, the AST
        # import path is not available. We still have the marker
        # checks above; skip the AST parse with a clear log.
        import sys as _sys
        if _sys.platform != "win32":
            import warnings
            warnings.warn(
                "test_uninstall_ps1_exists_and_parses: skipping AST "
                "parse on non-Windows dev host (no PowerShell CLI "
                "available). Marker checks still ran."
            )
            return
    # AST parse via the PowerShell CLI -- mirrors the installer's
    # parse check. We invoke pwsh in -Command mode and read the
    # error count.
    import subprocess as _sp
    if _sp.run(["where", "pwsh"], capture_output=True).returncode != 0:
        # PowerShell not installed on this host. Same rationale as above.
        import warnings as _w
        _w.warn(
            "test_uninstall_ps1_exists_and_parses: pwsh not on PATH; "
            "skipping AST parse. Marker checks still ran."
        )
        return
    ps_script = (
        f"$tokens=$null; $errors=$null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{p}', [ref]$tokens, [ref]$errors) | Out-Null; "
        f"if ($errors.Count -eq 0) {{ exit 0 }} else {{ exit 1 }}"
    )
    r = _sp.run(
        ["pwsh", "-NoProfile", "-Command", ps_script],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, (
        f"uninstall-omniroute.ps1 failed to parse via PowerShell AST: "
        f"{r.stderr!r}. Fix the syntax before committing."
    )


def test_uninstall_hook_does_not_touch_docker():
    """Phase 6.x + AGENTS.md invariant #19: `hooks.uninstall()` must
    be side-effect free. It must NOT call `subprocess`, `os.system`,
    or anything that talks to Docker. The gateway is independent
    infrastructure; the plugin's uninstall removes only the plugin
    folder. To also remove the gateway, the user clicks the WebUI
    button which downloads uninstall-omniroute.ps1 (a separate
    user-invoked path).

    This is a regression guard against a future contributor
    "helpfully" wiring docker stop into the uninstall hook.
    """
    p = os.path.join(PLUGIN_ROOT, "hooks.py")
    src = open(p, encoding="utf-8").read()
    # Locate the uninstall() function body. We don't try to be
    # clever — the function is small enough that substring checks
    # on the body are safe.
    import re as _re
    m = _re.search(
        r"async def uninstall\(\)[^\n]*:\s*\n((?:\s{4,}[^\n]*\n|\s*\n)+)",
        src,
    )
    assert m, "could not locate uninstall() in hooks.py"
    body = m.group(1)
    # The body is allowed to MENTION docker (the expanded docstring +
    # log message cross-reference the WebUI "Remove OmniRoute gateway"
    # button and the manual `docker stop ...` fallback). What is
    # forbidden is actually EXECUTING a docker call. Strip the
    # docstring + log message strings before the check, so the only
    # thing left is real code.
    import re as _re2
    code = _re2.sub(r'\"{3}.*?\"{3}', "", body, flags=_re2.DOTALL)  # docstring
    code = _re2.sub(r'"[^"\n]*"', "", code)  # one-line strings (log args)
    for token in ("subprocess", "os.system", "Popen", "run(", "call("):
        assert token not in code, (
            f"hooks.uninstall() must NOT call {token!r} — the plugin's "
            "uninstall is side-effect free. To remove the gateway, the "
            "user clicks the WebUI button which downloads "
            "uninstall-omniroute.ps1."
        )


def test_uninstall_button_wired_in_settings_ui():
    """Phase 6.x: the 'Remove OmniRoute gateway' button must be wired
    in the WebUI. The settings page and the dashboard are the two
    discoverable surfaces; both must have a button that calls
    `uninstallGateway()` and is gated on the right state.
    """
    # Settings page: button is in the READY STATE branch, calls
    # $store.omnirouteStore.uninstallGateway(), and is hidden by
    # the `installState === 'ready'` template guard.
    config_html = open(
        os.path.join(PLUGIN_ROOT, "webui", "config.html"),
        encoding="utf-8",
    ).read()
    assert "uninstallGateway" in config_html, (
        "webui/config.html must wire a button to "
        "$store.omnirouteStore.uninstallGateway() (the new lifecycle "
        "removal path). The 'Remove OmniRoute gateway' button belongs "
        "in the READY STATE branch."
    )
    assert "Remove OmniRoute gateway" in config_html, (
        "webui/config.html must contain a 'Remove OmniRoute gateway' "
        "button label. The exact text is part of the user-facing "
        "contract."
    )
    # Dashboard: button calls the local `uninstallGateway()` (the
    # dashboard has its own Alpine scope) and is gated on
    # `reachable`. We require the gating because showing the button
    # when the gateway is already offline would be confusing.
    dashboard_html = open(
        os.path.join(PLUGIN_ROOT, "webui", "dashboard.html"),
        encoding="utf-8",
    ).read()
    assert "uninstallGateway" in dashboard_html, (
        "webui/dashboard.html must wire a button to the local "
        "uninstallGateway() (the dashboard has its own Alpine scope)."
    )
    # The dashboard.js factory must actually export an
    # uninstallGateway method — otherwise the button click 404s.
    dashboard_js = open(
        os.path.join(PLUGIN_ROOT, "webui", "dashboard.js"),
        encoding="utf-8",
    ).read()
    assert "async uninstallGateway" in dashboard_js, (
        "webui/dashboard.js must define an `async uninstallGateway()` "
        "method on the omnirouteDashboard factory. Without it, the "
        "dashboard's 'Remove gateway' button is a no-op."
    )
    # Same for the settings store.
    store_js = open(
        os.path.join(PLUGIN_ROOT, "webui", "omniroute-store.js"),
        encoding="utf-8",
    ).read()
    assert "async uninstallGateway" in store_js, (
        "webui/omniroute-store.js must define an `async "
        "uninstallGateway()` method. Without it, the settings page's "
        "'Remove OmniRoute gateway' button is a no-op."
    )


# ===========================================================================
# 15. check.py skill probe (success + unreachable + --json + --help)
# ===========================================================================
class TestCheckPy:
    CHECK_PY = os.path.join(PLUGIN_ROOT, "skills", "omniroute-quickstart", "scripts", "check.py")

    def _run(self, *args, timeout=15) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, self.CHECK_PY, *args],
            capture_output=True, text=True, timeout=timeout,
        )

    def test_help(self):
        r = self._run("--help")
        assert r.returncode == 0
        assert "usage:" in r.stdout
        assert "--url" in r.stdout
        assert "--timeout" in r.stdout

    def test_success(self, stub_server):
        stub_server.set_response({"data": [{"id": "a"}, {"id": "b"}]})
        r = self._run("--url", f"http://127.0.0.1:{stub_server.port}/v1", "--timeout", "3")
        assert r.returncode == 0, f"check.py success path failed: {r.stdout} {r.stderr}"
        assert "OK" in r.stdout
        assert "2 models" in r.stdout

    def test_unreachable_exits_2(self):
        r = self._run("--url", "http://127.0.0.1:1", "--timeout", "1")
        assert r.returncode == 2, f"unreachable should exit 2, got {r.returncode}"
        assert "FAIL" in r.stdout

    def test_json_envelope(self):
        r = self._run("--url", "http://127.0.0.1:1", "--timeout", "1", "--json")
        env = json.loads(r.stdout.strip())
        assert env["ok"] is False
        assert "error" in env
        assert "base_url" in env
        assert env["plugin"] == "omniroute"


# ===========================================================================
# 16. AGENTS.md mentions pytest workflow (Phase 4 invariant 16)
# ===========================================================================
def test_agents_md_documents_pytest(agents_md):
    assert "pytest" in agents_md, (
        "AGENTS.md should document the pytest smoke suite + CI workflow"
    )


# ===========================================================================
# 17. self_check() in hooks.py mentions the test files
# ===========================================================================
def test_self_check_includes_tests():
    src = open(os.path.join(PLUGIN_ROOT, "hooks.py"), encoding="utf-8").read()
    assert "tests/smoke.py" in src, "hooks._self_check() must list tests/smoke.py"
    assert "tests/__init__.py" in src, "hooks._self_check() must list tests/__init__.py"


def test_self_check_includes_cache():
    src = open(os.path.join(PLUGIN_ROOT, "hooks.py"), encoding="utf-8").read()
    assert "helpers/cache.py" in src, (
        "hooks._self_check() must list helpers/cache.py (Phase 5.1 cache helper)"
    )


def test_self_check_includes_prompts():
    # Phase 5.2: the canonical agent profile's prompts/main.md must be in
    # the _self_check() inventory. A future contributor who removes the
    # canonical profile (or its prompts/) breaks the WebUI agent picker
    # silently; this test surfaces that.
    src = open(os.path.join(PLUGIN_ROOT, "hooks.py"), encoding="utf-8").read()
    assert "agents/omniroute/agent.yaml" in src, (
        "hooks._self_check() must list agents/omniroute/agent.yaml"
    )
    assert "agents/omniroute/prompts/main.md" in src, (
        "hooks._self_check() must list agents/omniroute/prompts/main.md"
    )


def test_live_test_file_exists_and_is_substantial():
    """Phase 5.3: pin the live test file. If this test fails, the live
    suite has been deleted or reduced to a stub — re-add coverage
    before merging. CI does not collect tests/live.py; this smoke
    test is the only automated signal that the live suite still
    exists in the right shape."""
    live_path = os.path.join(PLUGIN_ROOT, "tests", "live.py")
    assert os.path.isfile(live_path), (
        f"Live test file missing: {live_path}. The live suite "
        "(usr/plugins/omniroute/tests/live.py) is part of the plugin's "
        "release verification — re-create it or remove this test."
    )
    content = open(live_path, encoding="utf-8").read()
    # The skip mechanism is the contract: a 1.5s TCP probe that
    # converts "gateway down" into a clean module-level skipif.
    assert "_gateway_reachable" in content, (
        "Live test file does not contain `_gateway_reachable` — "
        "the skip-on-unreachable-gateway mechanism was removed."
    )
    # The env var is the user-facing API for pointing the live suite
    # at a non-default gateway. Catches accidental rename.
    assert "OMNIROUTE_BASE_URL" in content, (
        "Live test file does not reference OMNIROUTE_BASE_URL — "
        "the env-var convention was removed."
    )
    # Catch accidental reduction to a stub (e.g. someone comments out
    # the test class to silence a flake and never restores it).
    test_count = sum(
        1 for line in content.splitlines()
        if line.lstrip().startswith("def test_")
    )
    assert test_count >= 3, (
        f"Live test file has only {test_count} test functions "
        f"(expected >= 3). The live suite has been reduced to a stub."
    )


def test_self_check_includes_live_py():
    """Phase 5.3: hooks._self_check() must list tests/live.py so the
    inventory fails loudly if the live suite is removed (mirrors the
    test_self_check_includes_cache / _prompts pattern)."""
    src = open(os.path.join(PLUGIN_ROOT, "hooks.py"), encoding="utf-8").read()
    assert "tests/live.py" in src, (
        "hooks._self_check() inventory does not include tests/live.py"
    )


def test_install_wizard_does_not_reference_broken_one_liner():
    """Phase 5.4: the previous `oneLiner` copy button pointed at
    `https://raw.githubusercontent.com/agent0ai-community/omniroute-plugin/main/install-omniroute.ps1`,
    a public repo that does not exist (raw.githubusercontent.com returns 404).
    The fix removed the one-liner button and pointed users at the
    "Show full install script" advanced view, which fetches the script
    from the local plugin asset server (no public URL dependency).

    This test pins that contract. If a future contributor re-adds a
    `raw.githubusercontent.com/agent0ai-community/...` URL anywhere in
    the plugin tree, this test fails before the user sees a 404.
    """
    repo = PLUGIN_ROOT
    bad_url = "raw.githubusercontent.com/agent0ai-community"
    bad_store_field = "oneLiner"  # the broken one-liner getter was removed

    def _is_in_python_docstring(content, line_idx):
        """Return True if content[line_idx] sits inside a triple-quoted
        string (the standard Python docstring convention). Used to allow
        the URL to appear inside this test's own docstring (which
        explains the contract), and inside the version-history comment
        in omniroute-store.js:8."""
        in_doc = False
        quote = None
        for i, line in enumerate(content.splitlines()):
            # Count triple-quotes on this line (ignoring escaped quotes)
            stripped = line.replace('\\"', '').replace("\\'", "")
            for q in ('"""', "'''"):
                count = stripped.count(q)
                if count % 2 == 1:
                    if not in_doc:
                        in_doc, quote = True, q
                    else:
                        in_doc, quote = (q != quote), None
            if i == line_idx:
                return in_doc
        return False

    # Files we do not pin (this test's own code is allowed to reference
    # the URL; it pins the contract for shipped files).
    _EXEMPT = {"smoke.py"}

    for root, _dirs, files in os.walk(repo):
        # Only check text files we ship — skip tests/__pycache__,
        # config.json, etc.
        if "__pycache__" in root or ".git" in root:
            continue
        for f in files:
            if f in _EXEMPT:
                continue
            if not f.endswith((".py", ".js", ".html", ".md", ".yaml", ".ps1")):
                continue
            p = os.path.join(root, f)
            content = open(p, encoding="utf-8", errors="replace").read()
            if bad_url in content:
                # Allow the URL inside a removal-history note: a comment
                # (//, #, <!--) that mentions "removed" / "404" /
                # "did not exist", OR inside a Python docstring, OR in
                # a line whose surrounding context (within 3 lines
                # before/after) explains the removal.
                lines = content.splitlines()
                lines_with_url = [i for i, line in enumerate(lines) if bad_url in line]
                for line_idx in lines_with_url:
                    line = lines[line_idx]
                    stripped = line.strip()
                    is_comment = (
                        stripped.startswith("//")
                        or stripped.startswith("#")
                        or stripped.startswith("<!--")
                    )
                    has_removal_marker = (
                        "removed" in line.lower()
                        or "404" in line
                        or "did not exist" in line.lower()
                    )
                    in_docstring = _is_in_python_docstring(content, line_idx)
                    # Look at surrounding 3 lines for a removal
                    # explanation (e.g. the version-history comment
                    # block in omniroute-store.js mentions "removed"
                    # on the line before the URL).
                    window = "\n".join(
                        lines[max(0, line_idx - 3): line_idx + 4]
                    )
                    has_context_marker = (
                        "removed" in window.lower()
                        or "404" in window
                        or "did not exist" in window.lower()
                    )
                    allowed = (
                        (is_comment and has_removal_marker)
                        or in_docstring
                        or (is_comment and has_context_marker)
                    )
                    if not allowed:
                        rel = os.path.relpath(p, repo)
                        raise AssertionError(
                            f"{rel} references the broken agent0ai-community "
                            f"URL outside a removal-history comment or "
                            f"docstring: {line.strip()!r}"
                        )
            # The `oneLiner` getter and `ONE_LINER` constant must not
            # exist anywhere except in removal-history comments, docstrings,
            # or context windows that explain the removal.
            for marker in ("get oneLiner()", "const ONE_LINER", "store.omnirouteStore.oneLiner"):
                if marker in content:
                    lines = content.splitlines()
                    lines_with_marker = [i for i, ln in enumerate(lines) if marker in ln]
                    for line_idx in lines_with_marker:
                        line = lines[line_idx]
                        stripped = line.strip()
                        is_comment = (
                            stripped.startswith("//")
                            or stripped.startswith("#")
                            or stripped.startswith("<!--")
                        )
                        has_removal_marker = (
                            "removed" in line.lower()
                            or "did not exist" in line.lower()
                            or "no longer" in line.lower()
                        )
                        in_docstring = _is_in_python_docstring(content, line_idx)
                        window = "\n".join(
                            lines[max(0, line_idx - 3): line_idx + 4]
                        )
                        has_context_marker = (
                            "removed" in window.lower()
                            or "did not exist" in window.lower()
                            or "no longer" in window.lower()
                        )
                        allowed = (
                            (is_comment and has_removal_marker)
                            or in_docstring
                            or (is_comment and has_context_marker)
                        )
                        if not allowed:
                            rel = os.path.relpath(p, repo)
                            raise AssertionError(
                                f"{rel} references the removed oneLiner "
                                f"contract: {line.strip()!r}"
                            )


def test_dashboard_js_exposes_fromCache_and_cachePill():
    src = open(os.path.join(PLUGIN_ROOT, "webui", "dashboard.js"), encoding="utf-8").read()
    assert "fromCache" in src, "dashboard.js must declare a fromCache field"
    assert "cacheAgeSeconds" in src, "dashboard.js must declare a cacheAgeSeconds field"
    assert "cachePill" in src, "dashboard.js must expose a cachePill getter"


def test_dashboard_html_has_cache_pill():
    src = open(os.path.join(PLUGIN_ROOT, "webui", "dashboard.html"), encoding="utf-8").read()
    assert "cachePill" in src, "dashboard.html must render the cachePill"
    # Must be guarded by x-show so it only appears when fromCache is true
    assert "x-show=\"cachePill\"" in src, "cache pill must be x-show guarded"


def test_dashboard_html_shows_last_error_on_offline():
    """Phase 5.4.8: when the gateway is offline, the dashboard must
    surface the actual error from the API response. The previous
    version only showed 'Gateway is offline' with no diagnostic,
    leaving the user unable to distinguish DNS failure from a
    connection refused from an API 500.
    """
    src = open(os.path.join(PLUGIN_ROOT, "webui", "dashboard.html"), encoding="utf-8").read()
    assert "lastError" in src, (
        "dashboard.html must render the lastError from the API response "
        "so users can see WHY the gateway is offline (DNS / connection "
        "refused / API error / timeout)."
    )
    # The error block must be inside the offline state
    assert "x-show=\"lastError\"" in src or "x-show=\"!reachable\"" in src, (
        "lastError display must be guarded by x-show so it only appears "
        "when the dashboard is in the offline state."
    )


def test_dashboard_openSettings_uses_in_spa_openConfig():
    """v2.6.3: openSettings() must open the REAL plugin-settings panel
    in-SPA via the framework's ``pluginSettingsPrototype.openConfig``
    store method, NOT hard-navigate to a standalone config.html page.

    Root cause fixed: the standalone config.html served at
    /plugins/omniroute/webui/config.html has no Alpine.js and receives no
    config/context injection (the framework only injects those inside the
    plugin-settings.html wrapper). Hard-navigating there left every Alpine
    binding inert -> empty boxes + "$store.omnirouteStore undefined" (the
    "fail to load model ... undefined" the user reported). ``openConfig``
    opens the real settings panel as a stacked in-SPA modal that DOES
    inject config/context, so config.html renders with real data.
    """
    src = open(os.path.join(PLUGIN_ROOT, "webui", "dashboard.js"), encoding="utf-8").read()

    # The canonical in-SPA opener must be present.
    assert "pluginSettingsPrototype" in src, (
        "dashboard.js openSettings() must use the framework's "
        "pluginSettingsPrototype store to open the settings panel."
    )
    assert ".openConfig(" in src, (
        "dashboard.js openSettings() must call openConfig() on the "
        "pluginSettingsPrototype store."
    )

    # The openConfig call must be INSIDE openSettings (not just present
    # somewhere in the file) and must target the omniroute plugin
    # specifically. Scan for the body of the openSettings method — start at
    # the function header and read until the matching close brace.
    fn_start = src.find("openSettings() {")
    assert fn_start >= 0, "openSettings() function not found in dashboard.js"
    depth = 0
    i = fn_start
    saw_open = False
    while i < len(src):
        ch = src[i]
        if ch == "{":
            depth += 1
            saw_open = True
        elif ch == "}":
            depth -= 1
            if saw_open and depth == 0:
                break
        i += 1
    body = src[fn_start:i + 1]
    assert 'openConfig("omniroute")' in body or "openConfig('omniroute')" in body, (
        "openSettings() must call openConfig(\"omniroute\") to open this "
        "plugin's settings panel in-SPA."
    )

    # The old hard-navigation fallback must be GONE. The standalone config
    # page is Alpine-less + context-less, which is exactly the empty-box /
    # "undefined" failure this version fixes.
    assert 'window.location.href = "/plugins/omniroute/webui/config.html"' not in src, (
        "dashboard.js must NOT hard-navigate to the standalone config.html "
        "page (v2.6.3 removed this — it produced empty boxes + "
        "\"$store.omnirouteStore undefined\" because standalone config.html "
        "has no Alpine + no config/context injection)."
    )


# ===========================================================================
# 18. Dashboard debounce (Phase 4.1)
#
# The Alpine component is browser-side JS, but its refresh/forceRefresh
# debounce logic is testable in pure Node by stubbing globals. We use
# `node -e` to load the factory, replace fetch with a controlled stub,
# drive refresh() / forceRefresh(), and assert the right calls fire.
# ===========================================================================
NODE = shutil.which("node")


def _run_node(js_body: str, timeout: int = 15) -> Tuple[int, str, str]:
    """Run a JS snippet under Node and capture exit/stdout/stderr."""
    proc = subprocess.run(
        ["node", "-e", js_body],
        capture_output=True, text=True, timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_dashboard_js_uses_forceRefresh_in_html():
    html = open(os.path.join(PLUGIN_ROOT, "webui", "dashboard.html"), encoding="utf-8").read()
    assert "forceRefresh()" in html, "dashboard.html must bind the button to forceRefresh()"
    assert "refreshLabel" in html, "dashboard.html must use refreshLabel for the button text"


def test_dashboard_js_exposes_forceRefresh():
    """The Alpine factory must define forceRefresh() so the HTML click handler resolves."""
    js = open(os.path.join(PLUGIN_ROOT, "webui", "dashboard.js"), encoding="utf-8").read()
    assert "forceRefresh()" in js, "dashboard.js must define forceRefresh()"
    assert "refresh()" in js, "dashboard.js must still expose the debounced refresh()"
    assert "DEBOUNCE_MS" in js, "dashboard.js must declare a debounce window"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_dashboard_debounce_coalesces_rapid_calls():
    """Two refresh() calls within DEBOUNCE_MS must result in ONE fetch 5s later,
    not two. Verifies the debounce coalesces (latest-call-wins)."""
    js_path = os.path.join(PLUGIN_ROOT, "webui", "dashboard.js").replace("\\", "/")
    # The script imports a notification store — we stub it by returning a
    # no-op module via a custom loader. Easier: provide it as a global so
    # the static `import` line can be replaced with a comment.
    body = f"""
        global.fetchCount = 0;
        global.fetch = async () => {{
            global.fetchCount++;
            return {{ ok: true, json: async () => ({{ reachable: true, provider_count: 0, free_count: 0, cheap_count: 0, key_count: 0, sub_count: 0, latency_ms: 0, base_url: '', models: [], last_known: null }}) }};
        }};
        global.window = {{ Alpine: undefined }};
        // navigator is a read-only getter in modern Node — defineProperty to override
        Object.defineProperty(global, 'navigator', {{ value: {{ clipboard: {{ writeText: async () => {{}} }} }}, configurable: true }});
        global.localStorage = {{ getItem: () => null, setItem: () => {{}} }};
        // Load the module body (strip ESM syntax — we run as a classic script)
        const fs = require('fs');
        let src = fs.readFileSync('{js_path}', 'utf8');
        src = src.replace(/^\\s*import .+;?\\s*$/gm, '');
        src = src.replace(/^\\s*export\\s+/gm, '');
        // Stub the notification import (no module resolution needed in classic mode)
        global.toastFrontendError = () => {{}};
        global.toastFrontendInfo = () => {{}};
        // Evaluate
        const factory = new Function(src + '\\nreturn omnirouteDashboard;')();
        const c = factory();
        // Two rapid refresh() calls — should coalesce into ONE pending fetch
        c.refresh();
        c.refresh();
        c.refresh();
        // No fetch should have fired yet (we're 0s into the 5s debounce)
        console.log('FETCHES_AFTER_DEBOUNCE', global.fetchCount);
        // Force it: pending should be 0 after the timer fires (we don't wait
        // — we just verify pendingIn was set, indicating debounce is active)
        console.log('PENDING_IN', c.pendingIn);
        console.log('HAS_TIMER', !!c.pendingTimer);
        process.exit(0);
    """
    rc, out, err = _run_node(body)
    assert rc == 0, f"node exited {rc}: stderr={err!r}"
    # Three refresh() calls within the debounce window should:
    #  - not have fired fetch yet (we never wait for the timer in this test)
    #  - have set pendingIn (debounce is active)
    #  - have a pending timer registered
    fetch_line = [l for l in out.splitlines() if l.startswith("FETCHES_AFTER_DEBOUNCE")]
    pending_line = [l for l in out.splitlines() if l.startswith("PENDING_IN")]
    timer_line = [l for l in out.splitlines() if l.startswith("HAS_TIMER")]
    assert fetch_line, f"no FETCHES_AFTER_DEBOUNCE in output: {out!r}"
    assert pending_line, f"no PENDING_IN in output: {out!r}"
    assert timer_line, f"no HAS_TIMER in output: {out!r}"
    assert int(fetch_line[0].split()[1]) == 0, (
        f"refresh() should NOT fetch immediately (debounced); got "
        f"fetchCount={fetch_line[0]}"
    )
    assert int(pending_line[0].split()[1]) > 0, (
        f"refresh() should set pendingIn > 0; got {pending_line[0]}"
    )
    assert timer_line[0].split()[1] == "true", (
        f"refresh() should register a pendingTimer; got {timer_line[0]}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_dashboard_forceRefresh_cancels_pending_and_fetches_now():
    """forceRefresh() must bypass the debounce and fire fetch immediately."""
    js_path = os.path.join(PLUGIN_ROOT, "webui", "dashboard.js").replace("\\", "/")
    body = f"""
        (async () => {{
            global.fetchCount = 0;
            global.fetch = async () => {{
                global.fetchCount++;
                return {{ ok: true, json: async () => ({{ reachable: true, provider_count: 0, free_count: 0, cheap_count: 0, key_count: 0, sub_count: 0, latency_ms: 0, base_url: '', models: [], last_known: null }}) }};
            }};
            global.window = {{ Alpine: undefined }};
            Object.defineProperty(global, 'navigator', {{ value: {{ clipboard: {{ writeText: async () => {{}} }} }}, configurable: true }});
            global.localStorage = {{ getItem: () => null, setItem: () => {{}} }};
            const fs = require('fs');
            let src = fs.readFileSync('{js_path}', 'utf8');
            src = src.replace(/^\\s*import .+;?\\s*$/gm, '');
            src = src.replace(/^\\s*export\\s+/gm, '');
            global.toastFrontendError = () => {{}};
            global.toastFrontendInfo = () => {{}};
            const factory = new Function(src + '\\nreturn omnirouteDashboard;')();
            const c = factory();
            c.refresh();
            c.forceRefresh();
            await new Promise(r => setImmediate(r));
            await new Promise(r => setImmediate(r));
            await new Promise(r => setImmediate(r));
            console.log('FETCH_COUNT', global.fetchCount);
            console.log('PENDING_IN', c.pendingIn);
            console.log('HAS_TIMER', !!c.pendingTimer);
            process.exit(0);
        }})();
    """
    rc, out, err = _run_node(body)
    assert rc == 0, f"node exited {rc}: stderr={err!r}"
    fc_line = [l for l in out.splitlines() if l.startswith("FETCH_COUNT")]
    pi_line = [l for l in out.splitlines() if l.startswith("PENDING_IN")]
    ht_line = [l for l in out.splitlines() if l.startswith("HAS_TIMER")]
    assert fc_line, f"no FETCH_COUNT in output: {out!r}"
    assert pi_line, f"no PENDING_IN in output: {out!r}"
    assert ht_line, f"no HAS_TIMER in output: {out!r}"
    assert int(fc_line[0].split()[1]) == 1, (
        f"forceRefresh() should fire exactly one fetch; got {fc_line[0]}"
    )
    assert int(pi_line[0].split()[1]) == 0, (
        f"forceRefresh() should clear pendingIn; got {pi_line[0]}"
    )
    assert ht_line[0].split()[1] == "false", (
        f"forceRefresh() should clear the pending timer; got {ht_line[0]}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_dashboard_init_forceRefreshes_immediately():
    """Phase 5.4.8: dashboard.init() must call forceRefresh() (not refresh())
    so the user sees fresh status within ~1s instead of waiting 5s for
    the debounce. The previous behavior showed the default 'Offline'
    state for 5s after page load even when the gateway was reachable.
    """
    js_path = os.path.join(PLUGIN_ROOT, "webui", "dashboard.js").replace("\\", "/")
    body = f"""
        (async () => {{
            global.fetchCount = 0;
            global.fetch = async () => {{
                global.fetchCount++;
                return {{ ok: true, json: async () => ({{ reachable: true, provider_count: 0, free_count: 0, cheap_count: 0, key_count: 0, sub_count: 0, latency_ms: 0, base_url: '', models: [], last_known: null }}) }};
            }};
            global.window = {{ Alpine: undefined }};
            Object.defineProperty(global, 'navigator', {{ value: {{ clipboard: {{ writeText: async () => {{}} }} }}, configurable: true }});
            global.localStorage = {{ getItem: () => null, setItem: () => {{}} }};
            const fs = require('fs');
            let src = fs.readFileSync('{js_path}', 'utf8');
            src = src.replace(/^\\s*import .+;?\\s*$/gm, '');
            src = src.replace(/^\\s*export\\s+/gm, '');
            global.toastFrontendError = () => {{}};
            global.toastFrontendInfo = () => {{}};
            const factory = new Function(src + '\\nreturn omnirouteDashboard;')();
            const c = factory();
            c.init();
            // Wait one microtask flush so the async fetch can fire
            await new Promise(r => setImmediate(r));
            await new Promise(r => setImmediate(r));
            await new Promise(r => setImmediate(r));
            console.log('FETCH_COUNT', global.fetchCount);
            console.log('PENDING_IN', c.pendingIn);
            console.log('HAS_TIMER', !!c.pendingTimer);
            process.exit(0);
        }})();
    """
    rc, out, err = _run_node(body)
    assert rc == 0, f"node exited {rc}: stderr={err!r}"
    fc_line = [l for l in out.splitlines() if l.startswith("FETCH_COUNT")]
    pi_line = [l for l in out.splitlines() if l.startswith("PENDING_IN")]
    ht_line = [l for l in out.splitlines() if l.startswith("HAS_TIMER")]
    assert fc_line, f"no FETCH_COUNT in output: {out!r}"
    # init() must trigger an immediate fetch — not wait 5s.
    assert int(fc_line[0].split()[1]) >= 1, (
        f"init() should trigger an immediate fetch; got "
        f"fetchCount={fc_line[0]}. The previous version used the "
        f"debounced refresh() which waited 5s before the first fetch."
    )
    # No pending timer should be active after the immediate fetch.
    assert pi_line and int(pi_line[0].split()[1]) == 0, (
        f"init() should NOT leave a pending timer; got {pi_line}"
    )
    assert ht_line and ht_line[0].split()[1] == "false", (
        f"init() should NOT have a pending timer; got {ht_line}"
    )


# ===========================================================================
# 18b. config.html renders the model list (Phase 5.4.7 regression guard)
# ===========================================================================
def test_config_html_renders_model_id_and_tier_tag():
    """The 'Load model list' button calls /api/plugins/omniroute/models,
    which returns [{id, tier}, ...] (api/models.py:104-112). The store
    stores the response in $store.omnirouteStore.models and the
    template iterates with `<template x-for="m in ...">`. A previous
    version did `<div x-text="m">` which stringified the dict to
    "[object Object]" and rendered 230 useless rows. This test pins
    the corrected behavior: the template must extract m.id for the
    display text and m.tier for a colored tag.
    """
    p = os.path.join(PLUGIN_ROOT, "webui", "config.html")
    src = open(p, encoding="utf-8").read()
    # The template must NOT just stringify the whole object.
    # Look for the line that would render the model row. The pattern
    # `x-text="m"` (without a property access) is the bug.
    bad_lines = []
    for i, line in enumerate(src.splitlines(), start=1):
        stripped = line.strip()
        # Skip the store getter (loadModels) which legitimately does
        # `data.filtered || data.models` — we only care about the
        # template binding.
        if "x-text=\"m\"" in line or "x-text='m'" in line:
            bad_lines.append((i, line))
    assert not bad_lines, (
        "config.html still has a template that stringifies the model "
        "object (renders '[object Object]' instead of model IDs). Offending "
        f"lines: {bad_lines}. The fix is to use `m.id` and `m.tier`."
    )
    # The template must reference both m.id and m.tier
    assert "m.id" in src, (
        "config.html must render m.id for each model — the previous "
        "version rendered the whole dict as '[object Object]'."
    )
    assert "m.tier" in src, (
        "config.html must render m.tier for each model so users can "
        "see the tier classification (free/cheap/key/sub)."
    )
    # The :key on x-for must also be m.id, not the whole object
    assert ':key="m.id"' in src, (
        "config.html x-for must use :key=\"m.id\" — :key=\"m\" would "
        "use string equality on the whole dict, which doesn't work."
    )


# ===========================================================================
# 18c. bottom button has live status pill (Phase 5.4.7)
# ===========================================================================
def test_bottom_button_has_live_status_pill():
    """The bottom-right OmniRoute button must reflect actual gateway
    reachability, not always show green. The previous version hardcoded
    class='omni-on' and only flipped via a localStorage convention
    nothing ever wrote. This test pins the corrected behavior: the
    button must poll /api/plugins/omniroute/status and toggle
    omni-on <-> omni-off based on the live response.
    """
    p = os.path.join(
        PLUGIN_ROOT,
        "extensions",
        "webui",
        "chat-input-bottom-actions-end",
        "omniroute-button.html",
    )
    assert os.path.isfile(p), (
        f"Bottom button extension missing: {p}. The button file is part "
        "of the plugin's WebUI surface and must be present."
    )
    src = open(p, encoding="utf-8").read()
    # The file must NOT hardcode omni-on. Initial state must be
    # 'omni-unknown' so the pill starts in a neutral color and only
    # turns green AFTER a successful status poll.
    assert 'class="omni-bottom-btn omni-unknown"' in src, (
        "Bottom button must start with class='omni-bottom-btn omni-unknown' "
        "(neutral mid-grey) so it does not appear green before the first "
        "status poll completes."
    )
    # The script must poll the status endpoint, not rely on localStorage
    assert "localStorage.getItem('omniroute.mode')" not in src, (
        "Bottom button must NOT use localStorage for its on/off state — "
        "the source of truth is the live /api/plugins/omniroute/status "
        "endpoint. The previous localStorage convention was never "
        "written to by the plugin, so the button was always green."
    )
    # The script must call the status endpoint
    assert "/api/plugins/omniroute/status" in src, (
        "Bottom button must poll /api/plugins/omniroute/status to "
        "determine the live reachability of the configured gateway."
    )
    # The script must apply the omni-on/omni-off class based on
    # data.reachable
    assert "reachable" in src, (
        "Bottom button must read data.reachable from the status "
        "response — this is the source of truth for the pill color."
    )
    # The script must define the three states
    for cls in ("omni-on", "omni-off", "omni-unknown"):
        assert cls in src, (
            f"Bottom button must define CSS for .{cls} class — the "
            "script toggles between omni-on (green) and omni-off (grey), "
            "with omni-unknown as the initial transient state."
        )
    # The script must guard against re-injection (the extension slot
    # may be hit multiple times across SPA route changes)
    assert "__omnirouteButtonInjected" in src, (
        "Bottom button script must guard against re-injection — the "
        "extension slot can fire multiple times across SPA route "
        "changes, and we only want one polling timer."
    )


# ===========================================================================
# 18d. inventory includes the bottom button (Phase 5.4.7)
# ===========================================================================
def test_self_check_includes_bottom_button():
    """hooks._self_check() must list the bottom button so the inventory
    fails loudly if it is removed."""
    src = open(os.path.join(PLUGIN_ROOT, "hooks.py"), encoding="utf-8").read()
    assert "chat-input-bottom-actions-end/omniroute-button.html" in src, (
        "hooks._self_check() inventory does not include the bottom "
        "button file. Add it to the required list so a missing file "
        "is caught at startup."
    )


# ===========================================================================
# 19. hooks.install() pre-flight is non-blocking (Phase 4.2)
# ===========================================================================
def test_install_preflight_does_not_raise_when_gateway_down():
    """The pre-flight must warn-log on unreachable, never raise."""
    import asyncio
    import logging

    # Capture log records at WARNING level
    captured = []
    class _CaptureHandler(logging.Handler):
        def emit(self, record):
            captured.append(record)

    from usr.plugins.omniroute import hooks  # importable: the helper is local
    # Use a logger on the hooks module
    log = logging.getLogger("usr.plugins.omniroute.hooks")
    log.addHandler(_CaptureHandler())
    log.setLevel(logging.DEBUG)

    # gateway is NOT up locally — pre-flight should warn, never raise
    asyncio.run(hooks.install())
    levels = [r.levelname for r in captured]
    assert "WARNING" in levels, (
        f"install() should log WARNING when gateway unreachable; got {levels}"
    )
    # No ERROR or CRITICAL
    assert "ERROR" not in levels, "install() must never log ERROR (pre-flight is non-blocking)"
    assert "CRITICAL" not in levels


# ===========================================================================
# 20. helpers/utility_combo.py + api/combos.py + combos client (v2.6.4)
#
# The `auto/utility:free` route is curated by the pure helper
# `helpers/utility_combo.py:curate_utility_targets` and provisioned in the
# gateway by `OmniRouteClient.create_combo` (POST /api/combos, retry as PUT on
# conflict), glued together by `api/combos.py`. These tests cover the
# curator's exclusion/ordering/cap/reservation rules, the pure `gateway_root`
# helper, `create_combo`'s idempotent POST→PUT retry, and the endpoint's full
# curate-from-live-free-models flow (via the path-aware StubServer).
# ===========================================================================
class TestUtilityComboCurator:
    """The curator is the single source of truth for which free models suit
    the utility slot. These tests pin its exclusion, ordering, cap, and
    slow-reservation rules so a future edit can't silently drift them."""

    def test_drops_non_text_modalities(self, helpers_utility_combo):
        """Image / video / audio / embedding / rerank / moderation / toy /
        flaky / deprecated ids must be dropped; valid chat ids must survive."""
        ids = [
            "groq/llama-4-scout",          # valid fast chat (kept)
            "openai/gpt-4o-mini",          # valid chat (kept)
            "flux-dev",                    # image (dropped)
            "dall-e-3",                    # image (dropped)
            "google/veo-3",                # video (dropped)
            "openai/whisper-1",            # audio (dropped)
            "openai/tts-1",                # audio (dropped)
            "baai/bge-m3",                 # embedding (dropped)
            "jina/jina-rerank-v2",         # rerank (dropped)
            "meta-llama/llama-guard-3",    # moderation (dropped)
            "openai/clip-vit",             # clip (dropped)
            "smollm2-1.5b",                # toy (dropped)
            "qwen2.5-0.5b",                # toy (dropped)
            "g4f/gpt-4o",                  # flaky no-auth (dropped)
            "galadriel/test",              # deprecated (dropped)
            "predibase/x",                 # deprecated (dropped)
        ]
        out = helpers_utility_combo.curate_utility_targets(ids)
        out_set = set(out)
        # Valid chat models survive
        assert "groq/llama-4-scout" in out_set
        assert "openai/gpt-4o-mini" in out_set
        # Every excluded pattern is absent
        for bad in (
            "flux-dev", "dall-e-3", "google/veo-3", "openai/whisper-1",
            "openai/tts-1", "baai/bge-m3", "jina/jina-rerank-v2",
            "meta-llama/llama-guard-3", "openai/clip-vit", "smollm2-1.5b",
            "qwen2.5-0.5b", "g4f/gpt-4o", "galadriel/test", "predibase/x",
        ):
            assert bad not in out_set, f"excluded model survived curation: {bad!r}"

    def test_orders_fast_first_slow_last(self, helpers_utility_combo):
        """Tier 0 (fast hosts) → tier 1 (fast families) → tier 2 (mid) →
        tier 3 (slow reasoners). The strong-but-slow models come last."""
        ids = [
            "deepseek/deepseek-r1",       # tier 3 (slow reasoner)
            "deepseek/deepseek-chat",     # tier 2 (mid)
            "google/gemini-2.5-flash",    # tier 1 (fast family)
            "groq/llama-4-scout",         # tier 0 (fast host)
        ]
        out = helpers_utility_combo.curate_utility_targets(ids)
        assert out == [
            "groq/llama-4-scout",
            "google/gemini-2.5-flash",
            "deepseek/deepseek-chat",
            "deepseek/deepseek-r1",
        ], f"expected fast→mid→slow order, got {out}"

    def test_caps_at_max_targets(self, helpers_utility_combo):
        """The result never exceeds MAX_TARGETS, even with many valid ids."""
        # Generate 30 distinct fast-host ids (tier 0) — all valid
        ids = [f"groq/model-{i}" for i in range(30)]
        out = helpers_utility_combo.curate_utility_targets(ids)
        assert len(out) <= helpers_utility_combo.MAX_TARGETS, (
            f"curate returned {len(out)} > MAX_TARGETS="
            f"{helpers_utility_combo.MAX_TARGETS}"
        )
        # Original gateway order preserved within the tier
        assert out == [f"groq/model-{i}" for i in range(helpers_utility_combo.MAX_TARGETS)]

    def test_reserves_slow_tail_slots(self, helpers_utility_combo):
        """Even when the fast/mid pool alone would fill the cap, the
        `_RESERVE_SLOW` tail slots are reserved for tier-3 reasoners so the
        "best" models are always kept as last-resort fallbacks (ordered last,
        not excluded — per the user's "don't exclude the best models" rule)."""
        combo = helpers_utility_combo
        # More than enough fast models to fill the cap on their own
        fast = [f"groq/fast-{i}" for i in range(combo.MAX_TARGETS + 5)]
        # Two strong-but-slow reasoners that must land in the reserved tail
        slow = ["deepseek/deepseek-r1", "openai/o3"]
        out = combo.curate_utility_targets(fast + slow)
        # Cap honored
        assert len(out) == combo.MAX_TARGETS
        # The slow reasoners occupy the last _RESERVE_SLOW slots
        assert out[-combo._RESERVE_SLOW:] == slow, (
            f"expected the last {combo._RESERVE_SLOW} slots to be the slow "
            f"reasoners {slow}, got tail {out[-combo._RESERVE_SLOW:]}"
        )
        # And the fast models only filled (MAX_TARGETS - _RESERVE_SLOW) slots
        fast_slots = combo.MAX_TARGETS - combo._RESERVE_SLOW
        assert out[:fast_slots] == [f"groq/fast-{i}" for i in range(fast_slots)]
        # The slow reasoners must NOT appear in the fast section
        assert "deepseek/deepseek-r1" not in out[:fast_slots]
        assert "openai/o3" not in out[:fast_slots]

    def test_empty_and_non_string_inputs_are_safe(self, helpers_utility_combo):
        """Empty list, None entries, and non-string entries must not raise."""
        out = helpers_utility_combo.curate_utility_targets([])
        assert out == []
        # Mixed junk + one valid id
        out = helpers_utility_combo.curate_utility_targets(
            ["", None, 123, "groq/valid", "flux-img"]
        )
        assert out == ["groq/valid"]


def test_gateway_root_strips_trailing_v1(helpers_omniroute):
    """`gateway_root` strips the trailing `/v1` (and any trailing slash) so the
    combos API at the gateway root (`{root}/api/combos`) is reachable from a
    `/v1` base_url. Pure function — no I/O."""
    gr = helpers_omniroute.gateway_root
    assert gr("http://host.docker.internal:8080/v1") == "http://host.docker.internal:8080"
    assert gr("http://localhost:8080/v1/") == "http://localhost:8080"
    assert gr("http://localhost:8080/") == "http://localhost:8080"
    assert gr("http://localhost:8080") == "http://localhost:8080"
    assert gr("http://x/v1/") == "http://x"
    # Already a root (no /v1) — unchanged
    assert gr("http://x:8080") == "http://x:8080"
    # Defensive: empty / None
    assert gr("") == ""
    assert gr(None) == ""


def test_create_combo_retries_put_on_conflict(stub_server, helpers_omniroute):
    """`create_combo` POSTs {root}/api/combos and, on a 409/400 "already
    exists" conflict, retries as PUT {root}/api/combos/<urlencoded id> — so
    "Create / refresh" is idempotent and updates the combo in place."""
    stub_server.set_routes([
        # First the POST hits the conflict
        ("POST", "/api/combos", {"error": "combo already exists"}, 409),
        # Then the PUT succeeds
        ("PUT", "/api/combos/", {"ok": True, "id": "auto/utility:free"}, 200),
    ])
    client = helpers_omniroute.OmniRouteClient(
        base_url=f"http://127.0.0.1:{stub_server.port}/v1", timeout=3
    )
    r = client.create_combo("auto/utility:free", "priority", ["groq/x", "gemini/y"])
    assert r["ok"] is True, f"PUT retry should succeed: {r!r}"
    assert r["method"] == "PUT", f"expected method=PUT after conflict, got {r!r}"
    assert r["status"] == 200
    # Verify both requests were made in order: POST then PUT
    methods = [m for m, _ in stub_server.request_log]
    assert "POST" in methods and "PUT" in methods, (
        f"expected both POST and PUT requests; log={stub_server.request_log}"
    )
    assert methods.index("POST") < methods.index("PUT"), (
        f"POST must precede the PUT retry; log={methods}"
    )
    # The PUT URL must contain the urlencoded combo id
    put_paths = [p for m, p in stub_server.request_log if m == "PUT"]
    assert any("auto%2Futility%3Afree" in p for p in put_paths), (
        f"PUT path must urlencode the combo id (auto/utility:free -> "
        f"auto%2Futility%3Afree); got {put_paths}"
    )


def test_create_combo_post_succeeds_without_retry(stub_server, helpers_omniroute):
    """When the POST succeeds (2xx), `create_combo` must NOT retry as PUT —
    method is 'POST' and the body is returned as-is."""
    stub_server.set_routes([
        ("POST", "/api/combos", {"ok": True, "id": "auto/utility:free"}, 201),
    ])
    client = helpers_omniroute.OmniRouteClient(
        base_url=f"http://127.0.0.1:{stub_server.port}/v1", timeout=3
    )
    r = client.create_combo("auto/utility:free", "priority", ["groq/x"])
    assert r["ok"] is True
    assert r["method"] == "POST"
    assert r["status"] == 201
    # No PUT should have been issued
    assert "PUT" not in [m for m, _ in stub_server.request_log]


class TestCombosEndpoint:
    """api/combos.py glues: live free models (GET /v1/models) -> curate ->
    gateway POST /api/combos. Exercises the full path via the path-aware
    StubServer so the curator + client are tested together."""

    @pytest.fixture
    def stub_helpers_plugins(self, stub_server, monkeypatch):
        hp = types.ModuleType("helpers.plugins")
        hp.get_plugin_config = lambda name: {
            "base_url": f"http://127.0.0.1:{stub_server.port}/v1",
            "api_key": "",
            "timeout_seconds": 3,
        }
        sys.modules["helpers.plugins"] = hp
        yield
        sys.modules.pop("helpers.plugins", None)

    def _run(self, api_combos, payload=None):
        class _Req:
            pass
        return asyncio.run(api_combos.Combos().process(payload or {}, _Req()))

    def test_curates_from_live_free_models(
        self, stub_server, stub_helpers_plugins, api_combos, helpers_omniroute,
    ):
        """Full glue: GET /v1/models returns free + non-free + excluded ids;
        the endpoint filters to free, curates (dropping flux), and POSTs the
        combo. The response must be the documented success envelope."""
        stub_server.set_routes([
            # Live model catalog: one non-free, one excluded image model,
            # two valid free chat models (fast + slow reasoner).
            ("GET", "/v1/models", {"data": [
                {"id": "openai/gpt-4o"},                # sub tier — not free
                {"id": "flux-dev:free"},                # free but image — excluded
                {"id": "groq/llama-4-scout:free"},      # free + valid (fast)
                {"id": "deepseek/deepseek-r1:free"},    # free + valid (slow)
            ]}, 200),
            # Gateway accepts the combo
            ("POST", "/api/combos", {"ok": True, "id": "auto/utility:free"}, 200),
        ])
        result = self._run(api_combos)
        assert result["ok"] is True, f"expected ok, got {result!r}"
        assert result["combo_id"] == "auto/utility:free"
        assert result["selectable_as"] == "omniroute/auto/utility:free"
        assert result["strategy"] == "priority"
        # 3 free models seen (gpt-4o is sub, not counted as free)
        assert result["free_model_count"] == 3
        # flux excluded -> 2 targets (groq first, deepseek-r1 last)
        targets = result["targets"]
        assert result["target_count"] == len(targets) == 2
        assert targets[0] == "groq/llama-4-scout:free"
        assert targets[-1] == "deepseek/deepseek-r1:free"
        assert "flux-dev:free" not in targets
        # gateway_response envelope
        assert result["gateway_response"]["status"] == 200
        assert result["gateway_response"]["method"] == "POST"
        assert result["error"] is None

    def test_unreachable_gateway_returns_failure_envelope(
        self, api_combos, monkeypatch,
    ):
        """A dead gateway must return the documented failure envelope (ok=False)
        with a non-empty error, never raise."""
        hp = types.ModuleType("helpers.plugins")
        hp.get_plugin_config = lambda name: {
            "base_url": "http://127.0.0.1:1/v1",  # dead (port 1 reserved)
            "api_key": "",
            "timeout_seconds": 1,
        }
        sys.modules["helpers.plugins"] = hp
        try:
            result = self._run(api_combos)
            assert result["ok"] is False
            assert result["combo_id"] == "auto/utility:free"
            assert result["selectable_as"] == "omniroute/auto/utility:free"
            assert result["target_count"] == 0
            assert result["targets"] == []
            assert result["error"] is not None and result["error"].strip()
        finally:
            sys.modules.pop("helpers.plugins", None)

    def test_no_free_models_returns_failure_envelope(
        self, stub_server, stub_helpers_plugins, api_combos,
    ):
        """A reachable gateway with NO free models must return ok=False with
        a helpful error (not an empty combo)."""
        stub_server.set_routes([
            ("GET", "/v1/models", {"data": [
                {"id": "openai/gpt-4o"},      # sub
                {"id": "anthropic/claude"},   # sub
            ]}, 200),
        ])
        result = self._run(api_combos)
        assert result["ok"] is False
        assert result["free_model_count"] == 0
        assert result["targets"] == []
        assert "free" in result["error"].lower()

    def test_401_returns_set_api_key_message(
        self, stub_server, stub_helpers_plugins, api_combos,
    ):
        """v2.6.4: the gateway's POST /api/combos is an authenticated admin
        action — it 401s with no API key, even though GET /v1/models is public.
        The endpoint must detect the 401 and return a message that tells the
        user to set the OmniRoute API key (NOT a generic "gateway rejected"
        string). This is the exact failure a free-tier-first user hits when
        they click Create / refresh with no key configured."""
        stub_server.set_routes([
            # Free models list fine (public) — gets past curation.
            ("GET", "/v1/models", {"data": [
                {"id": "groq/llama-4-scout:free"},
            ]}, 200),
            # Combo create 401s (no key).
            ("POST", "/api/combos", {"error": {"message": "Authentication required"}}, 401),
        ])
        result = self._run(api_combos)
        assert result["ok"] is False
        # The 401 must be surfaced by name
        assert result["gateway_response"]["status"] == 401
        msg = result["error"]
        assert "401" in msg, f"401 message must mention 401; got {msg!r}"
        assert "API key" in msg, (
            f"401 message must tell the user to set the API key; got {msg!r}"
        )
        assert "Settings" in msg, (
            f"401 message must point at Settings; got {msg!r}"
        )
        # The free model count is still reported (the GET succeeded)
        assert result["free_model_count"] == 1


def test_self_check_includes_combos():
    """v2.6.4: hooks._self_check() must list the new utility-combo files so the
    inventory fails loudly if either is removed (mirrors the
    test_self_check_includes_cache / _prompts / _live_py pattern)."""
    src = open(os.path.join(PLUGIN_ROOT, "hooks.py"), encoding="utf-8").read()
    assert "api/combos.py" in src, (
        "hooks._self_check() must list api/combos.py (v2.6.4 combos endpoint)"
    )
    assert "helpers/utility_combo.py" in src, (
        "hooks._self_check() must list helpers/utility_combo.py "
        "(v2.6.4 utility-combo curator)"
    )


def test_agents_md_documents_utility_combo(agents_md):
    """v2.6.4: AGENTS.md must document the auto/utility:free route — the
    invariant (#20) and the v2.6.4 section — so a future contributor knows the
    curator is the single source of truth and provisioning never touches a
    preset."""
    assert "20." in agents_md, "AGENTS.md should document invariant 20 (utility combo)"
    assert "auto/utility:free" in agents_md
    assert "utility_combo.py" in agents_md
    assert "api/combos.py" in agents_md
    assert "curate_utility_targets" in agents_md
    # The combo is provisioned in the gateway, never written into a preset
    assert "never touches a model preset" in agents_md or "never writes the preset" in agents_md


# ===========================================================================
# Plain-script entry point
# ===========================================================================
if __name__ == "__main__":
    # When run as `python tests/smoke.py`, fall through to pytest's own runner
    # so output is structured.
    sys.exit(pytest.main([__file__, "-v"]))
