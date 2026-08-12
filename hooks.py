"""
OmniRoute plugin - lifecycle hooks.

Runs inside the Agent Zero framework runtime (not the agent execution
environment). The plugin installer calls `install()` after the plugin
is placed in usr/plugins/. The updater calls `pre_update()` before
pulling new code. The uninstaller calls `uninstall()` before the
plugin directory is deleted.

This plugin is intentionally side-effect free: it does not install pip
packages, does not start background services, and does not write
outside its own folder. OmniRoute itself runs as a separate Docker
container managed by the user; we only describe how to talk to it.

`install()` performs a NON-BLOCKING pre-flight: it probes the default
gateway host/port and logs a WARNING if unreachable. The install still
succeeds — the gateway can be started later. Per AGENTS.md invariant 15,
a failed probe is never an install failure.
"""

import importlib
import logging
import os

log = logging.getLogger(__name__)

PLUGIN_NAME = "omniroute"
EXPECTED_VERSION = "2.6.6"

# B2: the LiteLLM model id injected into a preset's ``kwargs.fallbacks`` when
# the user toggles "Use OmniRoute as a fallback". ``omniroute/auto`` lets the
# gateway pick the upstream per request (its 4-tier internal fallback).
OMNIROUTE_FALLBACK_MODEL = "omniroute/auto"


async def install() -> None:
    """Called after the plugin is placed in usr/plugins/.

    Non-blocking pre-flight: probes the default gateway host/port and
    logs a WARNING if unreachable. Does NOT raise — the install still
    succeeds even when the gateway is down. The user can configure the
    `base_url` in Settings or start the gateway at their leisure.
    """
    log.info(
        "[%s] install() called (v%s) - configure base_url in Settings -> External",
        PLUGIN_NAME,
        EXPECTED_VERSION,
    )
    # Lazy import so a partial framework never breaks hook load.
    try:
        from usr.plugins.omniroute.helpers.omniroute_client import (  # type: ignore
            _tcp_probe,
        )
        from urllib.parse import urlparse
    except Exception as e:  # pragma: no cover
        log.warning("[%s] pre-flight skipped (helper unavailable): %s", PLUGIN_NAME, e)
        return

    # Probe only the default host/port — not user-configured (that may not
    # exist yet). host.docker.internal:8080 is the documented default.
    host, port = "host.docker.internal", 8080
    try:
        reachable = _tcp_probe(host, port, timeout=1.5)
    except Exception as e:  # pragma: no cover
        reachable = False
        log.debug("[%s] probe raised (treating as unreachable): %s", PLUGIN_NAME, e)

    if reachable:
        log.info("[%s] pre-flight OK: gateway reachable at %s:%d", PLUGIN_NAME, host, port)
    else:
        log.warning(
            "[%s] pre-flight: gateway NOT reachable at %s:%d. "
            "The plugin will work as soon as you start the OmniRoute Docker "
            "container (or edit base_url in Settings -> External). "
            "See the dashboard for live status.",
            PLUGIN_NAME, host, port,
        )


async def pre_update() -> None:
    """Called immediately before the updater pulls new plugin code.

    Nothing to preserve: the plugin stores no per-install state. The
    user config in config.json lives outside the plugin directory and
    survives the update automatically.
    """
    log.info("[%s] pre_update() called - no state to migrate", PLUGIN_NAME)


async def uninstall() -> None:
    """Called before the plugin directory is deleted.

    SIDE-EFFECT FREE. The plugin folder will be removed by the
    framework after this returns (helpers/plugins.py:382-386 calls
    `uninstall_plugin()` -> `call_plugin_hook('uninstall')` ->
    `delete_plugin()`). The OmniRoute Docker container is
    intentionally left running: it is independent infrastructure that
    may be in use by other tools on this host (curl, scripts, other
    A0 plugins). Per AGENTS.md invariant #19, we do not stop or
    remove the container from this hook.

    To also remove the gateway, the user clicks the "Remove OmniRoute
    gateway" button in the WebUI (Settings -> External -> OmniRoute)
    BEFORE uninstalling the plugin. That button downloads
    `webui/uninstall-omniroute.ps1` for the user to run, which does
    `docker stop` + `docker rm` + an optional `docker rmi` of the
    image. If the user uninstalls the plugin first and then wants to
    clean up the container, they can run `docker stop omniroute &&
    docker rm omniroute` directly, or reinstall the plugin and use
    the WebUI button.
    """
    log.info(
        "[%s] uninstall() called - no cleanup required. "
        "The plugin folder will be removed by the framework. "
        "The OmniRoute Docker container is intentionally left running "
        "so other tools on this host (curl, scripts, other A0 plugins) "
        "keep working. To also remove the container, open Settings -> "
        "External -> OmniRoute and click 'Remove OmniRoute gateway' "
        "BEFORE uninstalling the plugin, or run "
        "`docker stop omniroute && docker rm omniroute` directly.",
        PLUGIN_NAME,
    )


def save_plugin_config(settings=None, project_name="", agent_profile="", **kwargs):
    """Reconcile ``omniroute/auto`` in the active model preset's fallback lists.

    Called by the framework (helpers/plugins.py:save_plugin_config) on every
    OmniRoute config save. Reads ``use_as_fallback_chat`` /
    ``use_as_fallback_utility`` from the settings being saved and adds (or
    removes) ``{"model": OMNIROUTE_FALLBACK_MODEL}`` in the matching slots of
    the active preset's ``kwargs.fallbacks`` — the list the _model_fallback
    cascade reads as candidate models (priority #1 in _build_candidates).

    Why a save hook and not a runtime *_model_call_before injection:
    _model_fallback's _00_strip_litellm_fallbacks strips ``fallbacks`` from
    model.kwargs right before the litellm call, and _build_candidates reads
    primary.kwargs["fallbacks"] BEFORE the *_model_call_before hooks run — so
    runtime injection would be stripped or arrive too late. The preset's
    kwargs.fallbacks is the correct, persisted source.

    Idempotent: toggling on twice never duplicates the entry; toggling off
    removes all matching entries; a save with the toggle already matching the
    current state does NOT rewrite presets.yaml (no spurious writes). Always
    returns ``settings`` so the OmniRoute config itself persists regardless of
    the preset side-effect, which is fully wrapped so a missing/unavailable
    _model_config never fails the config save.
    """
    settings = settings if isinstance(settings, dict) else {}
    try:
        chat_on = bool(settings.get("use_as_fallback_chat", False))
        utility_on = bool(settings.get("use_as_fallback_utility", False))
        _reconcile_omniroute_fallback(chat_on, utility_on, project_name, agent_profile)
    except Exception as e:  # pragma: no cover - never fail the config save
        log.warning(
            "[%s] fallback-toggle reconcile skipped: %s", PLUGIN_NAME, e,
        )
    return settings


def _reconcile_omniroute_fallback(chat_on, utility_on, project_name, agent_profile):
    """Add/remove ``omniroute/auto`` in the active preset's fallback lists.

    Mutates the preset in place within the ``presets`` list and writes the
    whole file via ``model_config.save_presets`` only when the membership of
    ``omniroute/auto`` actually changed in some slot.
    """
    # Defensive import: _model_config may be absent or not on the path.
    model_config = None
    for modpath in (
        "usr.plugins._model_config.helpers.model_config",
        "plugins._model_config.helpers.model_config",
    ):
        try:
            model_config = importlib.import_module(modpath)
            break
        except Exception:
            continue
    if model_config is None:
        log.info(
            "[%s] _model_config unavailable; fallback toggle is a no-op.",
            PLUGIN_NAME,
        )
        return

    name = model_config.get_configured_preset_name(
        project_name=project_name or None,
        agent_profile=agent_profile or None,
    )
    presets = model_config.get_presets()
    target = next(
        (p for p in presets
         if isinstance(p, dict) and str(p.get("name", "")).strip() == name),
        None,
    )
    if target is None:
        log.info(
            "[%s] active preset '%s' not found; fallback toggle is a no-op.",
            PLUGIN_NAME, name,
        )
        return

    changed = False
    for slot, on in (("chat", chat_on), ("utility", utility_on)):
        slot_cfg = target.get(slot)
        if not isinstance(slot_cfg, dict):
            continue
        kwargs = slot_cfg.get("kwargs")
        if not isinstance(kwargs, dict):
            kwargs = {}
            slot_cfg["kwargs"] = kwargs
        fbs = _coerce_fallbacks_list(kwargs.get("fallbacks"))
        has_omni = any(
            isinstance(e, dict) and e.get("model") == OMNIROUTE_FALLBACK_MODEL
            for e in fbs
        )
        if has_omni == bool(on):
            # Already in the desired state — do not rewrite presets.yaml.
            continue
        if on:
            fbs.append({"model": OMNIROUTE_FALLBACK_MODEL})
        else:
            fbs = [
                e for e in fbs
                if not (isinstance(e, dict) and e.get("model") == OMNIROUTE_FALLBACK_MODEL)
            ]
        kwargs["fallbacks"] = fbs
        changed = True

    if changed:
        model_config.save_presets(presets)
        log.info(
            "[%s] preset '%s' fallbacks updated (chat=%s, utility=%s).",
            PLUGIN_NAME, name, chat_on, utility_on,
        )
    else:
        log.debug("[%s] preset '%s' fallbacks unchanged.", PLUGIN_NAME, name)


def _coerce_fallbacks_list(value):
    """Coerce a ``kwargs.fallbacks`` value to a clean list of {model, api_base?}.

    Handles both stored forms: a native list (most presets) and a
    JSON-encoded string (the Default preset stores
    ``fallbacks: '[{"model":"..."}]'``). Drops entries without a non-empty
    string ``model``.
    """
    if isinstance(value, list):
        out = []
        for e in value:
            if isinstance(e, dict) and isinstance(e.get("model"), str) and e["model"].strip():
                entry = {"model": e["model"].strip()}
                api_base = e.get("api_base")
                if isinstance(api_base, str) and api_base.strip():
                    entry["api_base"] = api_base.strip()
                out.append(entry)
        return out
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        import json as _json
        try:
            parsed = _json.loads(s)
        except Exception:
            return []
        return _coerce_fallbacks_list(parsed)
    return []


def _self_check() -> dict:
    """Optional helper used by execute.py to verify the plugin is healthy."""
    here = os.path.dirname(os.path.abspath(__file__))
    required = [
        "plugin.yaml",
        "default_config.yaml",
        "hooks.py",
        "execute.py",
        "conf/model_providers.yaml",
        "agents/omniroute/agent.yaml",
        "agents/omniroute/prompts/main.md",
        # Note: agents/omniroute_safe/ is INTENTIONALLY absent. It was a legacy
        # v2.2 profile with no prompts/ directory and would render as a broken
        # SubAgent entry in the WebUI picker. Removed in v2.5.1 (AGENTS.md
        # invariant #18 — exactly one agent profile per plugin).
        "api/status.py",
        "api/models.py",
        "api/test.py",
        "api/dashboard.py",
        "api/usage.py",
        "api/combos.py",  # v2.6.4: provisions auto/utility-free in the gateway
        "helpers/omniroute_client.py",
        "helpers/last_known.py",
        "helpers/cache.py",
        "helpers/utility_combo.py",  # v2.6.4: curates the auto/utility-free target list
        "webui/config.html",
        "webui/omniroute-store.js",
        "webui/dashboard.html",
        "webui/dashboard.js",
        "webui/install-omniroute.ps1",
        "webui/uninstall-omniroute.ps1",  # Phase 6.x: gateway removal, downloaded by the WebUI button
        "extensions/webui/page-head/omniroute-status.html",
        "extensions/webui/sidebar-end/dashboard-link.html",
        "extensions/webui/chat-input-bottom-actions-end/omniroute-button.html",  # Phase 5.4.7: live status pill
        "skills/omniroute-quickstart/SKILL.md",
        "skills/omniroute-quickstart/scripts/check.py",
        "tests/__init__.py",
        "tests/smoke.py",
        "tests/live.py",  # Phase 5.3: live-gateway tests, run by hand before releases
    ]
    return {
        "plugin_dir": here,
        "version": EXPECTED_VERSION,
        "required_files": {p: os.path.isfile(os.path.join(here, p)) for p in required},
    }
