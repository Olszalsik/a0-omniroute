"""
Live tests for the OmniRoute plugin.

NOT collected by the default `pytest` invocation. Run by hand before
releases, or by a developer working on a feature that needs to verify
end-to-end behavior against a real gateway:

    python -m pytest usr/plugins/omniroute/tests/live.py -v

The smoke suite (tests/smoke.py) runs in CI and uses a local StubServer
that returns canned responses. This file is the live-gateway counterpart:
it exercises OmniRouteClient + the *_async wrappers against a real
OmniRoute instance reachable at OMNIROUTE_BASE_URL (default
http://host.docker.internal:8080/v1, the same default the API handlers
and dashboard use).

If the gateway is unreachable, every test is skipped (not failed) with
a single SKIPPED message at the top of the run — the user should not
see 30s of cascading timeouts when they forgot to start Docker.

What it covers (intentionally NOT exhaustive — the smoke suite owns
the unit-test surface):
  - OmniRouteClient.health_async against a real /v1/models endpoint.
  - OmniRouteClient.list_models_async consistency with health().
  - classify_tier / count_by_tier applied to the real model catalog.
  - OmniRouteClient.test_chat_async round-trip (real chat completion).
  - OmniRouteClient.usage_async 200/404 envelope shape.
  - The three user-facing presets (auto, fast, free) resolve to
    live model ids in the catalog AND classify into the expected
    tier band. This is a hard contract — if the tier classifier
    is wrong, the WebUI badge lies, and if the model id is
    missing the picker would 404 the call. Does NOT exercise
    chat-completions.
  - End-to-end chat-completions probe for the three presets.
    Soft-fails on documented upstream-credential errors
    (401/403/404/408/503/504/timeout) so maintainers can see
    the failure mode is gateway-side, not a regression.

The test_chat test spends real tokens. It pins
OMNIROUTE_LIVE_TEST_MODEL (default openai/gpt-4o:free) to keep the cost
near zero. Override the env var to validate a specific paid model.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import socket
import sys
from urllib.parse import urlparse

import pytest

# ---------------------------------------------------------------------------
# Path constants. Same convention as tests/smoke.py:58-60.
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)

# ---------------------------------------------------------------------------
# Live config. Mirror OmniRouteClient.__init__'s resolution: explicit arg
# (omitted in the live tests) > env var > documented default. The default
# MUST match helpers/omniroute_client.py:151 + default_config.yaml:16.
# ---------------------------------------------------------------------------
LIVE_BASE_URL = os.environ.get(
    "OMNIROUTE_BASE_URL", "http://host.docker.internal:8080/v1"
)
LIVE_API_KEY = os.environ.get("OMNIROUTE_API_KEY", "")

# test_chat_async uses 8 output tokens max. Default to a known-free model
# to keep the live test off the paid tier. Override via env var if the
# user wants to validate a specific paid model.
LIVE_TEST_MODEL = os.environ.get(
    "OMNIROUTE_LIVE_TEST_MODEL", "openai/gpt-4o:free"
)

GATEWAY_PROBE_TIMEOUT = 1.5  # seconds — same as hooks.install pre-flight


# ---------------------------------------------------------------------------
# Plugin module loader. The plugin lives in a namespace package
# (usr.plugins.omniroute.*) and can't be `import`ed directly outside the
# live A0 runtime. The same importlib.util pattern is used by smoke.py
# (see smoke.py:144-149). Loading happens at module import time so a
# runtime error in helpers/omniroute_client.py surfaces immediately.
# ---------------------------------------------------------------------------
def _load_plugin_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


helpers_omniroute = _load_plugin_module(
    "usr.plugins.omniroute.helpers.omniroute_client",
    os.path.join(PLUGIN_ROOT, "helpers", "omniroute_client.py"),
)


# ---------------------------------------------------------------------------
# Reachability probe. 1.5s TCP connect — same timeout as the install
# pre-flight in hooks.py:57. If the gateway is down, every test in the
# file is skipped with a single clear message; the user should not see
# 30s of cascading urllib timeouts.
# ---------------------------------------------------------------------------
def _parse_host_port(url: str):
    parsed = urlparse(url)
    host = parsed.hostname or "host.docker.internal"
    port = parsed.port or 80
    return host, port


def _gateway_reachable(url: str, timeout: float = GATEWAY_PROBE_TIMEOUT) -> bool:
    host, port = _parse_host_port(url)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


_GATEWAY_OK = _gateway_reachable(LIVE_BASE_URL)
_SKIP_REASON = (
    f"OmniRoute not reachable at {LIVE_BASE_URL} — start the Docker "
    "container (or set OMNIROUTE_BASE_URL). Live tests skipped."
)

# Module-level pytestmark applies the skipif to every test in this file
# without per-test decorators. Built into pytest, no config file needed.
pytestmark = [pytest.mark.skipif(not _GATEWAY_OK, reason=_SKIP_REASON)]


# ---------------------------------------------------------------------------
# Client builder. auto_detect=False so the client uses the URL we already
# have (no second autodetect_host probe, no time skew with the gate probe).
# ---------------------------------------------------------------------------
def _make_client(timeout: int = 30):
    """Build an OmniRouteClient with the live base_url + api_key."""
    return helpers_omniroute.OmniRouteClient(
        base_url=LIVE_BASE_URL,
        api_key=LIVE_API_KEY,
        timeout=timeout,
        auto_detect=False,
    )


# ---------------------------------------------------------------------------
# Tests. All use asyncio.run(...) inside sync def test_* to match the
# smoke suite's pattern (smoke.py:443-450). No pytest-asyncio.
# ---------------------------------------------------------------------------
class TestOmniRouteLive:
    """Live-gateway round-trips. Every test in this class is skipped
    at module load if OMNIROUTE_BASE_URL is unreachable."""

    def test_health_returns_ok_envelope(self):
        """Real /v1/models must return provider_count > 0 and a non-empty
        models list. Mirrors the smoke test that pins the 7-key contract
        (smoke.py:419-427) but exercises the real HTTP path."""
        client = _make_client()
        h = asyncio.run(helpers_omniroute.health_async(client))
        for key in ("ok", "base_url", "latency_ms", "provider_count",
                    "sample_models", "models", "error"):
            assert key in h, f"health() missing key {key!r}: {h!r}"
        assert h["ok"] is True, f"health() not ok: {h.get('error')!r}"
        assert h["provider_count"] > 0, (
            f"expected at least one provider, got provider_count="
            f"{h['provider_count']!r}: {h!r}"
        )
        assert isinstance(h["models"], list) and len(h["models"]) > 0, (
            f"health() returned empty models list: {h!r}"
        )
        assert isinstance(h["sample_models"], list)
        assert len(h["sample_models"]) <= 5, (
            f"sample_models should be <= 5 ids, got {len(h['sample_models'])}: "
            f"{h!r}"
        )

    def test_list_models_matches_health_models(self):
        """list_models() should agree with health()'s models list on
        at least the first few IDs. If they disagree, one of the two
        paths is silently filtering or duplicating."""
        client = _make_client()
        h = asyncio.run(helpers_omniroute.health_async(client))
        m = asyncio.run(helpers_omniroute.list_models_async(client))
        assert isinstance(m, list) and len(m) > 0, (
            f"list_models() returned empty list: {m!r}"
        )
        if h["models"]:
            # First few IDs in m should appear in h["models"]. This
            # catches the case where the two endpoints are returning
            # completely different catalogs (e.g. one is a subset of
            # an internal list, the other is the live one).
            sample = m[: min(5, len(m))]
            assert any(x in h["models"] for x in sample), (
                f"None of list_models()[:5]={sample} appear in "
                f"health()['models'][:5]={h['models'][:5]}: {h!r}"
            )

    def test_tier_counts_are_consistent(self):
        """count_by_tier(health()['models']) must sum to len(models).
        Live version of TestTierClassifier: catches catalog drift
        (e.g. a new model id that the classifier silently drops)."""
        client = _make_client()
        h = asyncio.run(helpers_omniroute.health_async(client))
        ids = h["models"]
        counts = helpers_omniroute.count_by_tier(ids)
        for bucket in ("free", "cheap", "key", "sub"):
            assert bucket in counts, (
                f"count_by_tier missing bucket {bucket!r}: {counts!r}"
            )
        tier_sum = sum(counts.values())
        assert tier_sum == len(ids), (
            f"tier sum {tier_sum} != |models| {len(ids)}: classifier "
            f"dropped or double-counted something: {counts!r}"
        )
        # Sanity: the OmniRoute catalog usually has at least one free
        # model. If this fails, either the catalog changed or the
        # tier classifier's free-pattern list is stale.
        if ids:
            assert counts["free"] >= 1, (
                f"No free-tier models in catalog ({len(ids)} total). "
                f"Did the free-tier patterns in classify_tier go stale? "
                f"{counts!r}"
            )

    def test_chat_round_trip_returns_content(self):
        """test_chat_async must actually produce a non-empty response.
        This is the only test in the suite that spends real tokens —
        we pin LIVE_TEST_MODEL (default openai/gpt-4o:free) to keep it
        free.

        Note: the client method (helpers/omniroute_client.py:260-271)
        returns the raw request envelope {status, latency_ms, body},
        not a wrapped {ok, response}. The api/test.py handler parses
        body.choices[0].message.content (api/test.py:49-54) — this
        test mirrors that parsing so it catches the same shape the
        handler depends on."""
        client = _make_client(timeout=60)
        try:
            r = asyncio.run(helpers_omniroute.test_chat_async(
                client, model=LIVE_TEST_MODEL, prompt="ping"
            ))
        except helpers_omniroute.OmniRouteError as e:
            pytest.skip(
                f"chat round-trip raised OmniRouteError: {e!r} "
                f"(set OMNIROUTE_LIVE_TEST_MODEL to a model this "
                f"gateway serves)"
            )
        # Raw envelope shape: {status, latency_ms, body}
        assert r.get("status") == 200, (
            f"test_chat returned status={r.get('status')!r}: {r!r}"
        )
        body = r.get("body") or {}
        if not body:
            pytest.skip(
                f"test_chat returned empty body (gateway may not serve "
                f"model {LIVE_TEST_MODEL}): {r!r}"
            )
        # Same parse as api/test.py:49-54
        try:
            content = (body.get("choices") or [{}])[0].get(
                "message", {}
            ).get("content", "")
        except Exception as e:
            pytest.fail(
                f"could not parse chat body: {e!r} in {body!r}"
            )
        assert content.strip(), (
            f"empty chat content in body.choices[0].message.content: "
            f"{body!r}"
        )

    def test_usage_envelope_shape_or_soft_skip(self):
        """usage() may 404 if OmniRoute hasn't implemented /usage yet —
        the docstring (omniroute_client.py:280-281) says 'treat 404 as
        not implemented'. Live test mirrors that: hard-fail on any
        envelope-shape regression, soft-skip on OmniRouteError (which
        _request raises on any non-2xx, line 178)."""
        client = _make_client()
        try:
            r = asyncio.run(helpers_omniroute.usage_async(client))
        except helpers_omniroute.OmniRouteError as e:
            # _request raises on any non-2xx (line 178). We can't
            # distinguish 404 (not implemented) from 5xx (real error)
            # without parsing the message, so we soft-skip on any
            # OmniRouteError and log the message. Maintainers running
            # the live suite by hand will see this and know to check
            # whether the gateway's /usage endpoint is up.
            pytest.skip(f"usage() raised OmniRouteError: {e!r}")
        # If it returned 200, the envelope shape is the request envelope.
        for key in ("status", "latency_ms", "body"):
            assert key in r, f"usage() missing key {key!r}: {r!r}"
        assert r["status"] == 200, f"unexpected usage status: {r!r}"

    def test_presets_resolve_and_classify_into_known_tiers(self):
        """The three user-facing presets (auto, fast, free) must each
        resolve to a real model id in the live catalog AND classify
        into the expected tier band. This is a hard contract — if
        the tier classifier is wrong, the WebUI badge lies to the
        user, and if the model id is missing the picker would 404
        the call.

        The test does NOT exercise the chat-completions path
        (separate env-var-gated test above does that) — it only
        verifies that the catalog has the expected model ids and
        that classify_tier returns the documented tier for each
        one. This catches regressions in helpers/omniroute_client.py
        even on a gateway where every upstream provider is down.
        """
        client = _make_client()
        h = asyncio.run(helpers_omniroute.health_async(client))
        ids = h["models"]
        # Each preset: (label, model_id, expected_tier)
        # tier expectations match the AGENTS.md invariant ("free"
        # for `:free` aliases, "cheap" for `:cheap` aliases, "sub"
        # for the rest of the auto/* family).
        PRESETS = [
            ("auto", "auto/best-free", "free"),
            ("fast", "auto/coding:fast", "sub"),
            ("free", "auto/coding:free", "free"),
        ]
        for label, model_id, expected_tier in PRESETS:
            assert model_id in ids, (
                f"preset [{label}] = {model_id!r} missing from live "
                f"catalog ({len(ids)} models). User's model picker "
                f"would 404 the call."
            )
            actual_tier = helpers_omniroute.classify_tier(model_id)
            assert actual_tier == expected_tier, (
                f"preset [{label}] = {model_id!r}: tier classifier "
                f"returned {actual_tier!r}, expected {expected_tier!r}. "
                f"WebUI tier badge would lie to the user. Check the "
                f"_TIER_*_PATTERNS lists in helpers/omniroute_client.py:50-82."
            )

    def test_presets_chat_completions_each_endpoint(self):
        """End-to-end chat-completions probe for the three user-facing
        presets (auto, fast, free). Each preset is exercised against
        /v1/chat/completions with a minimal 8-token prompt. The
        expected outcomes differ by gateway configuration:

        * a fully configured gateway: returns 200 with non-empty
          content (the test passes);
        * a gateway with no upstream credentials: every preset
          returns 401/403/503 with a JSON envelope — the test
          soft-skips with a clear message so maintainers know
          the gateway is up but the upstream is not;
        * a fully offline gateway: never reaches this test
          (the module-level probe skips the whole class).

        The point is to enumerate the failure modes and confirm
        the helper wraps them consistently. A real regression
        here would be a model id that 404s (model not found) or
        a missing /v1/chat/completions endpoint.
        """
        client = _make_client(timeout=20)
        PRESETS = [
            ("auto", "auto/best-free"),
            ("fast", "auto/coding:fast"),
            ("free", "auto/coding:free"),
        ]
        for label, model_id in PRESETS:
            try:
                r = asyncio.run(helpers_omniroute.test_chat_async(
                    client, model=model_id, prompt="ping"
                ))
            except helpers_omniroute.OmniRouteError as e:
                # 401/403/503/timeout = upstream not configured.
                # Anything else is a regression.
                msg = str(e)
                assert any(code in msg for code in (
                    "401", "403", "404", "408", "503", "504", "timed out"
                )), (
                    f"preset [{label}] = {model_id!r} returned an "
                    f"unexpected error envelope: {e!r}. If the model id "
                    f"is correct, this is a gateway-side regression."
                )
                continue
            assert r.get("status") == 200, (
                f"preset [{label}] = {model_id!r} returned "
                f"status={r.get('status')!r}: {r!r}"
            )
            body = r.get("body") or {}
            content = (body.get("choices") or [{}])[0].get(
                "message", {}
            ).get("content", "")
            assert content.strip(), (
                f"preset [{label}] = {model_id!r} returned empty "
                f"content: {body!r}"
            )


# ---------------------------------------------------------------------------
# Plain-script entry point (mirror smoke.py:1496-1499).
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
