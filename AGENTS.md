# AGENTS.md — operating contract for `a0-omniroute-plugin`

You are working on an Agent Zero plugin that connects A0 to [OmniRoute](https://github.com/diegosouzapw/OmniRoute),
a local AI gateway that exposes 230+ LLM providers through one OpenAI-compatible `/v1/*` endpoint with
4-tier fallback (Subscription → API Key → Cheap → Free). The plugin is **side-effect free**: no pip
installs, no background services, no writes outside its own folder. OmniRoute itself is a separate
Docker container managed by the user; this plugin only describes how to talk to it.

A mistake here causes a 60-second freeze of the whole A0 HTTP server (sync urllib call on the
single asyncio event loop), a misleading "0 providers" status badge, a broken Settings button, or
a 404 from the model picker. Follow these rules.

## What this plugin is
A self-contained A0 plugin (id `omniroute`, version `2.5.4`):

- A v2.5 `model_providers.yaml` entry so the model picker offers an "OmniRoute (auto)" choice
  (`conf/model_providers.yaml`).
- A `chat` agent profile at `agents/omniroute/agent.yaml` that uses the OmniRoute provider.
- Five async API handlers under `api/` — `status`, `models`, `test`, `dashboard`, `usage` — each
  wrapping a synchronous urllib call in `asyncio.to_thread` so A0's single event loop never blocks.
- A stdlib-only HTTP client at `helpers/omniroute_client.py` with `OmniRouteClient`, a shared tier
  classifier (`classify_tier` / `tier_sort_key` / `count_by_tier`), and a `*_async` wrapper for
  every blocking method.
- An Alpine `plugin-settings-store`-compatible WebUI (`webui/config.html` + `webui/omniroute-store.js`)
  registered via `<x-component path="/plugins/omniroute/webui/config.html">`.
- A WebUI status badge (`extensions/webui/page-head/omniroute-status.html`) + a chat-input button
  that opens a separate dashboard page (`webui/dashboard.html`).
- A PowerShell installer (`webui/install-omniroute.ps1`) that pulls the OmniRoute Docker image
  onto the host. Fetched at runtime from the asset server, never bundled into the framework.
- A PowerShell uninstaller (`webui/uninstall-omniroute.ps1`) that stops and removes the
  OmniRoute Docker container and optionally removes the image. Mirror of the installer:
  fetched at runtime from the asset server, never bundled. Downloaded only when the user
  clicks the WebUI "Remove OmniRoute gateway" button — never auto-run from any hook.
- A preconfigured `agents/omniroute/` profile with a `prompts/main.md` system prompt that
  teaches the agent OmniRoute's tier-fallback workflow.
- A `skills/omniroute-quickstart/` skill (SKILL.md + `scripts/check.py` stdlib probe) that
  the agent activates when the user asks how to bring the gateway online or fix a connection.
- A persistent model-list cache at `helpers/cache.py` (plugin-local `config.json["models_cache"]`,
  atomic write via the same helper as `last_known.py`) that lets the dashboard render instantly
  on page load and fall back gracefully when the live gateway is down.

## HARD INVARIANTS — never violate
1. **API handlers must call `*_async` wrappers, not sync `client.<method>`.** A0 v2.5 runs all API
   handlers on a single asyncio event loop (`helpers/api.py:80` does `output = await self.process(...)`).
   Calling `client.health()`, `client.list_models()`, `client.test_chat()`, or `client.usage()`
   directly from `async def process(...)` blocks the entire A0 HTTP server for the full request
   timeout (default 60s) and freezes every other agent's UI. Use `health_async`, `list_models_async`,
   `test_chat_async`, `usage_async` — they wrap the sync calls in `asyncio.to_thread`.
2. **Async wrappers only exist in the helper.** If you need a new HTTP method on `OmniRouteClient`,
   add the sync method AND a matching `*_async` wrapper. Don't bypass it via
   `asyncio.to_thread(client._request, ...)` from the API handler — that path is for one-off helpers
   inside the helper itself.
3. **Tier classification is single-sourced in the helper.** `classify_tier`, `tier_sort_key`, and
   `count_by_tier` live in `helpers/omniroute_client.py`. Every handler (dashboard, usage) and the
   dashboard UI share these. Don't reintroduce local `_FREE_PATTERNS` / `_classify_tier` copies
   in `api/*.py` — they WILL drift.
4. **`health()` is the only place that calls `/v1/models` per request.** It returns the full list
   under `models: []` so all callers can derive counts without a second round trip. Don't add
   `await list_models_async(client)` after a `health_async` call — the data is already there.
4a. **`api/models.py` returns `[{id, tier}, ...]`, NOT flat strings.** The shape matches
    `api/dashboard.py` so every UI (model picker, dashboard, future panels) renders tier
    coloring with the same helper. `tier` is one of `free | cheap | key | sub` (from
    `classify_tier`). The list is sorted free → cheap → key → sub, alphabetical within tier.
    If a new UI needs the model list, call this handler — don't add a third `/v1/models`
    round trip somewhere else.
5. **The `default_config.yaml` `api_base` is a literal URL — no `{config.*}` placeholders.** The
   A0 v2.5 `model_providers.yaml` schema (`helpers/providers.py:71-78`) does NOT interpolate
   `{config.*}` into `api_base`. The default (`http://host.docker.internal:8080/v1`) is hardcoded.
   Users who need a different URL must edit `conf/model_providers.yaml` directly, or set
   `OMNIROUTE_BASE_URL` env var AND update the YAML.
6. **Default `base_url` is `http://host.docker.internal:8080/v1` everywhere.** Port 8080 (NOT
   20128) because port 20128 (the upstream OmniRoute image's `ENV PORT`) is commonly blocked
   on Windows dev machines — symptom: "Empty reply from server" in `curl.exe` and
   `ERR_SOCKET_NOT_CONNECTED` in browsers. Port 8080 is allowed by Windows Firewall/AV by
   default. The PowerShell installer publishes host 8080 -> container 20128 (NOT 2012 — the
   previous install-omniroute.ps1 used 2012 and the container would start but no process would
   bind, producing a misleading "Gateway did not respond" error). Use the SAME default in
   `default_config.yaml`, every `_resolve_config()` fallback in `api/*.py`, `execute.py`, and
   `conf/model_providers.yaml`.
7. **The Settings button MUST use the v2.5 Alpine store.** Open settings via
   `window.Alpine?.store("pluginSettingsPrototype")?.openConfig("omniroute")` (fall back to
   `window.location.href = "/usr/plugins/omniroute/webui/config.html"`). Do NOT dispatch an
   `open-plugin-settings` CustomEvent or call a non-existent `window.openPluginSettings` —
   both no-op in v2.5.
8. **The PowerShell installer is fetched from `/plugins/omniroute/webui/install-omniroute.ps1`,
   NOT from `/usr/plugins/...`.** v2.5's asset server (`helpers/ui_server.py:316-319`) serves
   built-in plugins from `/plugins/<name>/` and user plugins from `/usr/plugins/<name>/`. The
   installer lives in `webui/`, which is served by BOTH routes (path-traversal guard allows
   `webui/` and `extensions/webui/`), but only `/plugins/...` is the version that survives
   reinstalls — the user-plugin route would 404 the file. Always fetch from `/plugins/...`.
9. **The chat-input button links to `/usr/plugins/omniroute/webui/dashboard.html`, NOT
   `/plugins/...`.** The dashboard is a plugin-local page, not a built-in asset, so the
   user-plugin route is correct here. (Mirror of the built-in/user asset rule.)
10. **Hook signatures are `async def install/pre_update/uninstall`.** A0 v2.5 awaits all three
    (see `usr/plugins/_model_fallback/hooks.py:5-11`). Defining them as sync `def` will trigger
    a `TypeError: object coroutine is not callable` from the installer and abort the install
    mid-way. Keep the `async` keyword even when the body is just a `log.info(...)`.
11. **All I/O from the WebUI is JSON via `fetch('/api/plugins/omniroute/<handler>', { method: 'POST' })`.**
    A0 v2.5 dispatches `api/<name>.py` files matching the trailing path segment. Don't try to
    expose the handlers under a different path — the framework won't find them.
12. **The plugin does NOT install OmniRoute.** It only describes how to talk to an already-running
    gateway. The PowerShell installer is a *helper* surfaced from the WebUI; it runs on the host
    via `fetch` and `Blob`/`URL.createObjectURL` — the A0 container never runs Docker. Don't
    add a `docker run` call to `hooks.py`.
13. **`last_known` is plugin-local and write-isolated.** `helpers/last_known.py` reads and writes
    ONLY `usr/plugins/omniroute/config.json` via an atomic tmp+rename. It never deletes or
    rewrites keys it didn't put there (preserves the user's `base_url`, `api_key`, etc.).
    The persisted snapshot gives the WebUI "last seen 3 min ago" feedback when the live
    gateway check fails. If a future feature needs cross-plugin state, it does NOT go here —
    add a new helper with its own file.
14. **The agent profile's `prompts/main.md` is the source of truth for OmniRoute routing
    guidance.** Don't duplicate the same guidance in the agent `description` (that's what
    users see in the WebUI picker — keep it short and human-readable), and don't add a
    "system context" string that paraphrases it. The prompt is loaded by
    `helpers/subagents.py:214-218` as `subagent.prompts["main"]` and is always in context
    while the agent runs. The companion `skills/omniroute-quickstart/` is matched on demand
    for the bring-the-gateway-online workflow.
15. **`install()` is non-blocking and never raises.** `hooks.install()` performs a 1.5-second
    TCP probe of the default gateway host/port (`host.docker.internal:8080`) and logs a
    WARNING (not ERROR) when the probe fails. A failed probe is NOT an install failure —
    the user can configure the gateway after install. If you tighten this into a hard
    error, you'll break installs for users who run the plugin without Docker (e.g. WSL,
    native macOS, air-gapped setups). The probe reuses `_tcp_probe` from the helper; do
    not roll your own socket logic.
16. **All plugin changes MUST pass the permanent pytest smoke suite.** Run
    `python -m pytest usr/plugins/omniroute/tests/smoke.py -v` from the repo root before
    committing. The CI workflow at `.github/workflows/omniroute-smoke.yml` enforces this
    for every PR and push to `v2.5` that touches `usr/plugins/omniroute/**`. The suite
    is the single source of truth for "is this plugin healthy?" — if you add a feature,
    add a test alongside it; if you remove a feature, remove its test. The suite must
    not depend on a real OmniRoute instance (use the `StubServer` fixture in
    `tests/smoke.py`).
17. **The model-list cache is a best-effort accelerator, never a source of truth.**
    `helpers/cache.py` persists the last full model snapshot to
    `config.json["models_cache"]` via the same atomic write helper as `last_known.py`
    (shared `_read_raw_config` / `_write_raw_config`). A stale, corrupt, missing, or
    version-mismatched cache MUST never cause an API call to fail or return an error
    — `api/dashboard.py` always attempts the live gateway first, and only falls back
    to the cache when the live call fails AND the cache's `base_url` matches the
    currently configured `base_url` (don't serve a stale cache from a different
    gateway the user used previously). If the cache write fails, the live response
    is still returned. The cache is dashboard-only; `api/models.py` and
    `api/status.py` do not consult it.
18. **Exactly one agent profile per plugin.** The canonical profile is
    `agents/omniroute/agent.yaml` with a sibling `prompts/main.md`. There is no
    `agents/omniroute_safe/`, no `agents/omniroute_*` sibling. Adding a second
    profile silently renders a broken SubAgent entry in the WebUI picker
    (`_get_agents_list_from_dir` at `helpers/subagents.py:89-110` iterates every
    subdirectory of `agents/` regardless of name). If you need a different profile,
    edit the canonical one in place or rename the directory — don't add a sibling.
    The agent profile's `prompts/main.md` is the source of truth for OmniRoute
    routing guidance: don't duplicate that guidance in the agent `description`
    (that's what users see in the WebUI picker — keep it short and human-readable),
    and don't add a "system context" string that paraphrases it. The prompt is
    loaded by `helpers/subagents.py:214-218` as `subagent.prompts["main"]` and is
    always in context while the agent runs. The companion
    `skills/omniroute-quickstart/` is matched on demand for the
    bring-the-gateway-online workflow.
19. **The plugin does NOT remove the OmniRoute container.** `hooks.uninstall()`
    removes only the plugin folder; the Docker container is independent
    infrastructure that may be in use by other tools on the host (curl, scripts,
    other A0 plugins). To remove the container, the user clicks the
    "Remove OmniRoute gateway" button in the WebUI (settings page or
    dashboard), which downloads `webui/uninstall-omniroute.ps1` for them to
    run. Never call `docker stop` / `docker rm` / `docker rmi` from
    `hooks.uninstall()` or from any other hook — the container survives
    the plugin. The user's two options after uninstalling the plugin are
    (a) reinstall the plugin and use the WebUI button, or (b) run
    `docker stop omniroute && docker rm omniroute` directly. This is the
    explicit-removal counterpart to invariant #12 (which forbids
    installing the gateway from `hooks.install()`). The two invariants
    together are the full side-effect-free contract.

## Build discipline
- **Stdlib-only helpers.** `helpers/omniroute_client.py` must not depend on `requests`, `httpx`,
  or `aiohttp` — Agent Zero's runtime doesn't ship them and adding a dependency for one plugin
  is the wrong trade.
- **Per change:** run `python -m py_compile` on every `.py` you touched; verify the WebUI still
  parses by importing `webui/omniroute-store.js` and `webui/dashboard.js` (strip `import`/`export`
  first — they break `new Function(src)`); keep `default_config.yaml` ↔ `webui/config.html` keys
  in sync. Bump `plugin.yaml` `version` on a release. If you add a new required file to the
  plugin, also add it to the `required` list in `hooks._self_check()` so the smoke suite
  catches a missing file as a regression (the suite is the single source of truth for
  "is this plugin healthy?").
- **Run the smoke suite before commit:** `python -m pytest usr/plugins/omniroute/tests/smoke.py -v`.
  The suite covers syntax, version agreement, hooks async, base_url defaults, tier classifier,
  `OmniRouteClient.health()` shape, `last_known` round-trip, `models_cache` round-trip, the
  dashboard handler's cache-first / live-always / fallback-on-failure flow, AGENTS.md invariant
  coverage, file inventory, and `check.py` exit codes. CI enforces the same check for every
  PR — don't merge a red build.
- **Run the live suite before release:** `python -m pytest
  usr/plugins/omniroute/tests/live.py -v`. Live tests are skipped
  in CI and skipped at runtime if `OMNIROUTE_BASE_URL` is unreachable
  (1.5s TCP probe at module load), so they're safe to run on a
  developer machine with the Docker container up. They exercise
  `OmniRouteClient.health_async` / `list_models_async` /
  `test_chat_async` / `usage_async` and the tier classifier against
  a real gateway, catching regressions the stubbed smoke suite
  cannot (e.g. `/v1/models` body-parser changes, free-tier catalog
  drift, paid-tier token spend). The `test_chat` test pins
  `OMNIROUTE_LIVE_TEST_MODEL` (default `openai/gpt-4o:free`) to
  avoid accidentally exercising a paid tier.
- **The agent profile's `prompts/main.md` is the source of truth for OmniRoute routing
  guidance.** If you remove the `prompts/` directory the profile renders as a broken
  entry in the agent picker — there is no UI warning, the framework just shows a
  SubAgent with no prompts. The smoke test `TestAgentProfile` pins this contract.
- **The cache key `models_cache` lives in `config.json` alongside `last_known`.** Don't move
  it to a separate file — they share the same atomic writer and the same lifecycle. A separate
  file would mean a second writer with its own tmp+rename and its own failure modes.
- **Keep THIS file current.** Update AGENTS.md in the SAME change whenever you alter a HARD
  INVARIANT, a cited path/seam/A0 mechanic, or what this plugin is. A stale contract MISLEADS
  (worse than none). Routine fixes/features that don't change the contract don't touch it.
- **Validate in a throwaway A0 instance.** Spin up a separate container with the plugin mounted,
  hit `/api/plugins/omniroute/status` with `curl -X POST`, and watch the framework logs for
  event-loop stalls. The maintainer installs the built artifact via the WebUI — don't live-install.
- **Opsec (public repo):** no `api_key` values, host IPs, internal hostnames, personal email,
  or local paths in shipped files. `config.json`, `.toggle-*`, `__pycache__/`, `execute_record.json`
  are gitignored. Commits: single human author, GitHub no-reply email, NO AI
  `Co-Authored-By` trailers.

## Knowledge map (one source of truth each — never duplicate)
- **User-facing install / quickstart / FAQ:** `README.md`.
- **Config defaults + inline rationale:** `default_config.yaml` (the canonical key list + meanings).
- **Manifest / version / settings UI surface:** `plugin.yaml`.
- **HTTP client + tier classifier + async wrappers:** `helpers/omniroute_client.py`.
- **Last-known-good persistence:** `helpers/last_known.py` (plugin-local config.json
  read/write; never touches anything outside the plugin folder).
- **Model-list cache (dashboard accelerator):** `helpers/cache.py` reads/writes
  `config.json["models_cache"]` (plugin-local, atomic via the same
  `_write_raw_config` helper as `last_known.py`; never touches anything outside
  the plugin folder). The cache and `last_known` share the on-disk file and the
  atomic writer — keep them in lockstep.
- **API endpoints (one handler per file):** `api/status.py`, `api/models.py`, `api/test.py`,
  `api/dashboard.py`, `api/usage.py`.
- **Settings UI (Alpine store contract):** `webui/config.html` + `webui/omniroute-store.js`.
- **Dashboard page:** `webui/dashboard.html` + `webui/dashboard.js`.
- **WebUI injection points:** `extensions/webui/page-head/omniroute-status.html` (status badge) +
  `extensions/webui/chat-input-bottom-actions-end/omniroute-button.html` (open-dashboard button) +
  `extensions/webui/sidebar-end/dashboard-link.html` (sidebar link).
- **Agent Zero model registration:** `conf/model_providers.yaml` + `agents/omniroute/agent.yaml`
  (the latter auto-picked by `helpers/subagents.py:71-72` via
  `get_enabled_plugin_paths(None, "agents")`; its `prompts:` map loads `*.md` files from
  the sibling `prompts/` directory per `helpers/subagents.py:214-218`). The canonical
  profile is **`agents/omniroute/`** — there is one and only one agent profile per
  plugin. Legacy variants (e.g. `agents/omniroute_safe/`) are explicitly removed; if
  you need a different profile, edit the canonical one in place, don't add a second.
- **Bring-the-gateway-online skill:** `skills/omniroute-quickstart/SKILL.md` +
  `skills/omniroute-quickstart/scripts/check.py` (stdlib-only probe reusing the same
  `OmniRouteClient` the API handlers use).
- **Self-check / maintenance script:** `execute.py` (run from Plugins UI or
  `python /a0/usr/plugins/omniroute/execute.py`).
- **Plugin lifecycle:** `hooks.py` (`async install/pre_update/uninstall`).

## Verified A0 v2.5 mechanics (don't re-derive — confirm against the LIVE instance; versions move)
- API dispatch: `helpers/api.py:206-272` resolves `POST /api/plugins/<name>/<handler>` →
  `usr/plugins/<name>/api/<handler>.py` → instantiates the `ApiHandler` subclass and awaits
  `self.process(input_data, request)`. The handler MUST be `async def process`.
- Asset server: `helpers/ui_server.py:316-319` guards path traversal; `webui/` and
  `extensions/webui/` are the only two subdirs served. Routes are `/plugins/<name>/<path>` (built-in)
  and `/usr/plugins/<name>/<path>` (user).
- WebUI config injection: `webui/components/plugins/plugin-settings-store.js:434-437` injects
  `<x-component path="/plugins/${name}/webui/config.html">` into the Settings page. The store
  contract is `init(config, context)`, `bindConfig(config)`, `cleanup()` — confirmed identical to
  `_oauth/webui/oauth-config-store.js`.
- Settings open: `Alpine.store("pluginSettingsPrototype").openConfig("omniroute")` is the
  v2.5 public API. There is no `window.openPluginSettings` and no `open-plugin-settings` event.
- Hooks: `usr/plugins/_model_fallback/hooks.py:5-11` confirms the async signature
  (`async def install()`, `async def pre_update()`, `async def uninstall()`).
- Provider merge: `helpers/providers.py:71-78` merges plugin `conf/model_providers.yaml` files.
  `api_base` is a literal string; `{config.*}` placeholders are NOT interpolated.
- Plugin agent profiles: `helpers/subagents.py:71-72` calls
  `get_enabled_plugin_paths(None, "agents")` to discover `agents/<profile>/agent.yaml` files.

## What this plugin does NOT do
- Does not start, stop, or manage the OmniRoute Docker container — only talks to it.
- Does not remove the OmniRoute container at plugin uninstall time. `hooks.uninstall()`
  is side-effect free; the container is independent infrastructure that may be in use
  by other tools on the host. To also remove the container, the user clicks the WebUI
  "Remove OmniRoute gateway" button (which downloads `webui/uninstall-omniroute.ps1`).
  The uninstall script is also never auto-run from any hook — the user always invokes
  it explicitly.
- Does not install pip packages.
- Does not write outside `usr/plugins/omniroute/` (no global config, no `~/.config/`, no
  `/usr/local/bin/`). **One approved exception:** the B2 `save_plugin_config` hook writes
  `omniroute/auto` into `usr/plugins/_model_config/presets.yaml` (see
  [B2 — Use OmniRoute as a fallback model](#b2--use-omniroute-as-a-fallback-model)). That is
  the only cross-plugin write, it touches only the `kwargs.fallbacks` list, and it is fully
  wrapped so it can never fail an OmniRoute config save.
- Does not register scheduled tasks or background workers.
- Does not modify Agent Zero framework code in `helpers/`, `webui/components/`, or `root/`.
- Does not expose any API key, host IP, or local path in shipped files.

---

## B2 — Use OmniRoute as a fallback model

**Added 2026-08-08 (omniroute v2.6.2 / B2 of the OmniRoute-fallback plan).** A
one-click convenience that injects `{"model": "omniroute/auto"}` into the
active model preset's chat and/or utility `kwargs.fallbacks` — the list the
`_model_fallback` cascade reads as candidate models (priority #1 in
`_build_candidates`, `fallback.py:466`). Both toggles default OFF (opt-in).

### Config

Two top-level keys in `default_config.yaml` (bound to checkboxes in
`webui/config.html` under Advanced settings, mirroring `preload_models`):

| Key | Default | Effect |
|---|---|---|
| `use_as_fallback_chat` | `false` | Append `omniroute/auto` to the active preset's **chat** `kwargs.fallbacks` |
| `use_as_fallback_utility` | `false` | Append `omniroute/auto` to the active preset's **utility** `kwargs.fallbacks` |

The framework's generic `save_plugin_config` flow persists these to
`usr/plugins/omniroute/config.json` and calls the `save_plugin_config` hook
(`helpers/plugins.py:645`).

### The `save_plugin_config` hook (`hooks.py`)

`save_plugin_config(settings, project_name, agent_profile)` runs on every
OmniRoute config save and reconciles `omniroute/auto` in the active preset:

1. Reads `use_as_fallback_chat` / `use_as_fallback_utility` from `settings`.
2. Imports `_model_config.helpers.model_config` (defensive: tries
   `usr.plugins._model_config...` then `plugins._model_config...`; if both
   fail, logs an INFO no-op and returns — the OmniRoute config still saves).
3. `name = get_configured_preset_name(project_name=, agent_profile=)`;
   `presets = get_presets()`; finds the preset named `name`.
4. For each slot where the toggle is ON: if no `omniroute/auto` entry exists,
   append `{"model": "omniroute/auto"}`. Where OFF: remove all
   `omniroute/auto` entries. The fallbacks value is coerced to a native list
   first (JSON-parsed if it's the JSON-string form the Default preset ships —
   `_coerce_fallbacks_list`).
5. `save_presets(presets)` **only if** the `omniroute/auto` membership actually
   changed in some slot — a save with the toggle already matching the current
   state does NOT rewrite `presets.yaml` (no spurious writes, no clobbering of
   unsaved preset-editor changes).
6. Always returns `settings` (so the OmniRoute config persists regardless of
   the preset side-effect, which is fully wrapped in try/except).

**Idempotent:** toggling on twice never duplicates the entry; toggling off
removes all matching entries; toggling off when none present is a no-op.

**Why a save hook, not a runtime `*_model_call_before` injection:**
`_model_fallback`'s `_00_strip_litellm_fallbacks` strips `fallbacks` from
`model.kwargs` right before the litellm call, and `_build_candidates` reads
`primary.kwargs["fallbacks"]` BEFORE the `*_model_call_before` hooks run — so
runtime injection would be stripped or arrive too late. The preset's
`kwargs.fallbacks` is the correct, persisted source.

### Cross-plugin write — the one approved exception

This is the only place the plugin writes outside `usr/plugins/omniroute/`:
it mutates `usr/plugins/_model_config/presets.yaml`. The write is scoped to
the `kwargs.fallbacks` list of the active preset's chat/utility slots only,
goes through `_model_config`'s own `save_presets` (which `validate_presets`-
cleans the whole file), and is wrapped so an OmniRoute config save never
fails if `_model_config` is unavailable. The same data is editable in the
`_model_config` preset editor's **Fallback models** picker (B1), so the two
stay consistent.

### Default fallback model

`omniroute/auto` — lets the gateway pick the upstream per request via its
4-tier internal fallback (Sub → Key → Cheap → Free). To use a specific
OmniRoute model id instead, toggle this on (which seeds the row), then edit
the row in the `_model_config` preset editor's Fallback models picker.

### Race note

`save_presets` rewrites the whole `presets.yaml`. If the preset editor is
open with unsaved changes when the user saves OmniRoute config, the
OmniRoute save wins (last writer). This is inherent to B2 being a shortcut
that writes the same presets file B1 edits. The membership-change-only guard
above minimizes the window: a no-op toggle save does not rewrite the file.
