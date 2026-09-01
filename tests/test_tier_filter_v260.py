"""Tests for the OmniRoute tier classification + filtering (v2.6.0+).

The tier system is composed of two pieces:

1. **Classifier** — ``helpers/omniroute_client.py::classify_tier`` (with
   ``tier_sort_key`` / ``count_by_tier``) is the single source of truth
   that labels every model id as ``free | cheap | key | sub``.
2. **API** — ``api/models.py`` classifies + sorts the gateway's model
   list, accepts an optional ``tier`` field in the request body and
   filters the returned ``filtered`` list to that tier. Substring and
   tier filters compose with AND. An unknown tier is a soft-degrade to
   "no filter" so a stale client can't hide every model.

The tier filter is surfaced in the WebUI by the inline dashboard
(``webui/dashboard.js`` ``tierFilter``), not by a separate chat-input
injector file. (An earlier draft of this file also tested
``chat-input-bottom-actions-end/tier-filter.html`` and a page-head
patch — those files never shipped and the tests were removed.)

These tests guard the classifier/API contract individually so a future
refactor of either is caught before it lands.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

# Portable: derive the plugin dir from this file's location
# (tests/ -> omniroute -> plugins -> usr -> repo root). The env override
# exists for out-of-tree checkouts.
PLUGIN_DIR = Path(os.environ.get(
    "OMNIROUTE_PLUGIN_ROOT",
    Path(__file__).resolve().parents[1],
))
MODELS_API = PLUGIN_DIR / "api" / "models.py"

# api/models.py (and the classifier sanity-checks below) do real
# ``usr.plugins.omniroute...`` package imports, which only resolve when
# the repo root is importable. Put it on sys.path ourselves so the
# tests run from any cwd (pytest -m only adds cwd, not the repo root).
_REPO_ROOT = str(PLUGIN_DIR.parents[2])
if os.path.isdir(_REPO_ROOT) and _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _read(p: Path) -> str:
    assert p.exists(), f"missing: {p}"
    return p.read_text(encoding="utf-8")


def _load_models_api():
    """Load ``api/models.py`` as an isolated module with a stub
    ``helpers.api.ApiHandler`` (the real one requires a Flask app
    and a thread lock we don't have in unit tests). This mirrors
    the pattern in ``tests/smoke.py::api_models``.

    Important: we only swap ``helpers.api``; we leave
    ``helpers.plugins`` (and any other real submodules) alone so
    other test files that import ``helpers.plugins.get_plugin_config``
    are not poisoned by our stub. We also save the original
    ``helpers`` and ``helpers.api`` entries and restore them on
    teardown, so a previous test that set them up differently
    is not silently overwritten.
    """
    saved_h = sys.modules.get("helpers")
    saved_h_api = sys.modules.get("helpers.api")

    h = types.ModuleType("helpers")
    h_api = types.ModuleType("helpers.api")

    # Carry over any attributes that other test files may rely on
    # (e.g. ``helpers.plugins``). ``sys.modules`` is the source of
    # truth, so we re-attach those submodules to the stub parent.
    for k, v in list(sys.modules.items()):
        if k == "helpers" or k.startswith("helpers."):
            setattr(h, k.split(".", 1)[-1] if "." in k else "__name__", v)
    h.__path__ = list(getattr(saved_h, "__path__", []) or [])

    class _StubApiHandler:
        def __init__(self, *a, **k):
            pass

    h_api.ApiHandler = _StubApiHandler
    sys.modules["helpers"] = h
    sys.modules["helpers.api"] = h_api

    name = "usr.plugins.omniroute.api.models"
    spec = importlib.util.spec_from_file_location(name, str(MODELS_API))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)

    # Restore the original helpers.* modules so subsequent tests
    # that import ``helpers.plugins`` (or anything else) keep
    # working. Without this, a follow-up test file would see the
    # stub and break with ``AttributeError: module 'helpers' has
    # no attribute 'plugins'``.
    if saved_h is not None:
        sys.modules["helpers"] = saved_h
    else:
        sys.modules.pop("helpers", None)
    if saved_h_api is not None:
        sys.modules["helpers.api"] = saved_h_api
    else:
        sys.modules.pop("helpers.api", None)

    return mod


# ---------------------------------------------------------------------------
# Plugin metadata
# ---------------------------------------------------------------------------

class TestVersionConsistency:
    def test_versions_agree(self):
        """plugin.yaml, hooks.py and execute.py must all declare the
        same version (one bump per additive change — smoke.py asserts
        the same invariant; this keeps the tier-filter file standalone)."""
        manifest = _read(PLUGIN_DIR / "plugin.yaml")
        m = re.search(r"^version:\s*(\S+)\s*$", manifest, re.MULTILINE)
        assert m, "plugin.yaml has no version: line"
        version = m.group(1)
        hooks_src = _read(PLUGIN_DIR / "hooks.py")
        execute_src = _read(PLUGIN_DIR / "execute.py")
        assert f'EXPECTED_VERSION = "{version}"' in hooks_src, (
            f"hooks.py EXPECTED_VERSION must match plugin.yaml version {version}"
        )
        assert f'EXPECTED_VERSION = "{version}"' in execute_src, (
            f"execute.py EXPECTED_VERSION must match plugin.yaml version {version}"
        )


# ---------------------------------------------------------------------------
# API: tier filter
# ---------------------------------------------------------------------------

class TestModelsApiTierFilter:
    """The API must accept ``tier`` in the request body and filter
    the result list. Substring and tier filters compose with AND.
    An unknown tier value is treated as "no tier filter" (i.e. all
    tiers are returned) so a client with a stale dropdown option
    does not silently get an empty list."""

    def _build_handler(self, raw_models, *, base_url="http://x", api_key="", timeout=30):
        api_mod = _load_models_api()
        # Patch the network call out: the handler depends on
        # ``health_async`` which talks to the gateway. Return a
        # pre-baked health payload.
        async def fake_health(client):
            return {"ok": True, "models": raw_models, "error": None}
        patcher_h = patch.object(api_mod, "health_async", fake_health)
        patcher_r = patch.object(api_mod, "_resolve_config",
                                  return_value=(base_url, api_key, timeout))
        patcher_h.start()
        patcher_r.start()
        return api_mod, api_mod.Models(), patcher_h, patcher_r

    def _run(self, api_mod, handler, input_data):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(handler.process(input_data, request=None))
        finally:
            loop.close()

    def test_no_filter_returns_all_tiers(self):
        api_mod, h, ph, pr = self._build_handler(["auto/best-free", "openai/gpt-4o", "oc/qwen-2.5-free"])
        try:
            out = self._run(api_mod, h, {})
            assert out["error"] is None
            # 3 raw -> 3 returned, none filtered out
            assert out["count"] == 3
            assert len(out["filtered"]) == 3
            # Echoes the (empty) tier back so the WebUI can confirm
            # what the server applied.
            assert out["tier"] == ""
        finally:
            ph.stop(); pr.stop()

    def test_tier_free_only(self):
        # Use ids the canonical Python classifier labels as "free"
        # so the API behavior is asserted against the same source
        # of truth.
        from usr.plugins.omniroute.helpers.omniroute_client import classify_tier
        free_ids = [
            "auto/best-free",       # free
            "veo-free/x",           # free
            "veoaifree-web/x",      # free
            "ddgw/x",               # free
        ]
        sub_ids = ["openai/gpt-4o"]
        # Sanity-check the test data against the classifier.
        for mid in free_ids:
            assert classify_tier(mid) == "free", f"test data drift: {mid}"
        for mid in sub_ids:
            assert classify_tier(mid) == "sub", f"test data drift: {mid}"

        api_mod, h, ph, pr = self._build_handler(free_ids + sub_ids)
        try:
            out = self._run(api_mod, h, {"tier": "free"})
            assert out["tier"] == "free"
            ids = [m["id"] for m in out["filtered"]]
            for mid in free_ids:
                assert mid in ids, f"missing free id {mid}"
            for mid in sub_ids:
                assert mid not in ids, f"unexpected sub id {mid}"
        finally:
            ph.stop(); pr.stop()

    def test_tier_sub_only(self):
        api_mod, h, ph, pr = self._build_handler(["auto/best-free", "openai/gpt-4o", "pepper/foo"])
        try:
            out = self._run(api_mod, h, {"tier": "sub"})
            ids = [m["id"] for m in out["filtered"]]
            # Both "openai/gpt-4o" (default -> sub) and "pepper/foo"
            # (pepper/ prefix -> sub) should be present.
            assert "openai/gpt-4o" in ids
            assert "pepper/foo" in ids
            assert "auto/best-free" not in ids
        finally:
            ph.stop(); pr.stop()

    def test_tier_cheap_only(self):
        api_mod, h, ph, pr = self._build_handler([
            "auto/cheap-thing",       # cheap (auto/cheap)
            "oc/deepseek-r1-flash",   # cheap (oc/deepseek-.*flash)
            "openai/gpt-4o",          # sub
        ])
        try:
            out = self._run(api_mod, h, {"tier": "cheap"})
            ids = [m["id"] for m in out["filtered"]]
            assert "auto/cheap-thing" in ids
            assert "oc/deepseek-r1-flash" in ids
            assert "openai/gpt-4o" not in ids
        finally:
            ph.stop(); pr.stop()

    def test_unknown_tier_is_no_filter(self):
        """A stale or malicious tier value (e.g. ``tier: "all"``)
        must NOT return an empty list — it's a soft-degrade to
        "no tier filter" so the WebUI dropdown can't accidentally
        hide every model."""
        api_mod, h, ph, pr = self._build_handler(["openai/gpt-4o", "auto/best-free"])
        try:
            out = self._run(api_mod, h, {"tier": "garbage"})
            assert out["tier"] == ""
            assert len(out["filtered"]) == 2
        finally:
            ph.stop(); pr.stop()

    def test_substring_and_tier_compose_with_and(self):
        from usr.plugins.omniroute.helpers.omniroute_client import classify_tier
        # Pick ids that the canonical classifier labels in a way
        # we can assert without drifting from production behavior.
        # "claude-sub" -> sub (default), "claude:free" -> free.
        candidates = [
            "claude-3:free",          # free
            "claude-3-sub",           # sub (default)
            "auto/claude",            # sub (auto/claude)
            "auto/cheap-claude",      # cheap
        ]
        # Sanity-check
        for mid, expected in [
            ("claude-3:free", "free"),
            ("claude-3-sub", "sub"),
            ("auto/claude", "sub"),
            ("auto/cheap-claude", "cheap"),
        ]:
            assert classify_tier(mid) == expected, f"classifier drift: {mid}"

        api_mod, h, ph, pr = self._build_handler(candidates)
        try:
            out = self._run(api_mod, h, {"filter": "claude", "tier": "sub"})
            ids = [m["id"] for m in out["filtered"]]
            assert "claude-3-sub" in ids
            assert "auto/claude" in ids
            assert "claude-3:free" not in ids
            assert "auto/cheap-claude" not in ids
        finally:
            ph.stop(); pr.stop()

    def test_empty_response_carries_tier_back(self):
        """When the gateway is unreachable the handler returns
        an empty shape. The ``tier`` field must still be echoed
        back so the WebUI can show "filter applied: free (0
        results)" instead of looking like a bug."""
        api_mod = _load_models_api()
        async def boom(client):
            from usr.plugins.omniroute.helpers.omniroute_client import OmniRouteError
            raise OmniRouteError("test down")
        ph = patch.object(api_mod, "health_async", boom)
        pr = patch.object(api_mod, "_resolve_config",
                          return_value=("http://x", "", 30))
        ph.start(); pr.start()
        try:
            handler = api_mod.Models()
            out = self._run(api_mod, handler, {"tier": "free"})
        finally:
            ph.stop(); pr.stop()
        assert out["error"] is not None
        assert out["tier"] == "free"   # echoed back
        assert out["filtered"] == []
        assert out["tier_counts"] == {"free": 0, "cheap": 0, "key": 0, "sub": 0}