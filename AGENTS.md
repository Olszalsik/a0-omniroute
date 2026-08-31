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
9. **The dashboard is shown INLINE on the Settings page (v2.6.6); the standalone
   `dashboard.html` modal is kept but no longer opened from the Settings page.** Since v2.6.5
   the chat-input bottom pill was a no-op (removed — it duplicated the status already shown on
   the plugin pages and opened the *internal* dashboard, which users confused with the
   gateway's own web UI). v2.6.6 then removed the Settings-page "Open dashboard" button too:
   the live dashboard state (model counts, tier breakdown bar, the `auto/utility-free` creator)
   is now rendered directly inside `webui/config.html`, bound to `$store.omnirouteStore` getters
   (`dashModelCount`, `pctFree`, …) populated by `loadDashboard()` (POST
   `/api/plugins/omniroute/dashboard`) and `createUtilityCombo()` (POST
   `/api/plugins/omniroute/combos`). The standalone `webui/dashboard.html` + `dashboard.js` are
   KEPT (they reuse the same `/dashboard` endpoint and are the surface for any future direct
   `openModal('/plugins/omniroute/webui/dashboard.html')` use); `openModal` is the framework
   global that loads a page in-SPA (Alpine + stores present) — never hard-navigate to a
   standalone `dashboard.html` (that would reload the SPA). The
   `extensions/webui/chat-input-bottom-actions-end/omniroute-button.html` file is KEPT as a
   no-op so the extension slot stays registered — do not re-add a button there.
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
20. **The `auto/utility-free` combo is curated server-side from live free models,
    and provisioning it never touches a model preset.** The combo is a
    gateway-side route (created via `POST /api/combos` on the gateway root, NOT
    `/v1`), provisioned by `api/combos.py` from the user's *live* free model list
    (one `GET /v1/models`) run through the pure curator
    `helpers/utility_combo.py:curate_utility_targets`. The curator is the ONE
    place that decides which free models suit the utility slot (fast +
    JSON-capable + a usable context window; image/audio/embedding/toy/flaky/
    deprecated ids dropped; `^auto/` meta-routes excluded so the combo never
    targets itself or another combo; best-but-slow reasoners kept as last-resort
    fallbacks, never excluded). The dashboard button only creates/refreshes
    the combo in the gateway — it does NOT write the user's preset; the user
    picks `omniroute/auto/utility-free` for the Utility slot themselves. The
    combo name is **colon-free** (`auto/utility-free`, NOT `auto/utility:free` —
    the gateway bans colons in names with a 400; the selectable LiteLLM id is
    the slugified name `omniroute/auto/utility-free`). The create call is
    **idempotent via upsert-by-name** (see the verified gateway combo contract
    in `helpers/omniroute_client.py:create_combo`): `GET /api/combos` → find the
    combo whose `name` matches → `PUT /api/combos/<UUID>` to refresh in place;
    if none exists, `POST /api/combos`. The gateway keys combos by its own UUID
    and ignores any `id`/`slug` we send, and a duplicate-name POST is rejected —
    so PUT-by-name (the old v2.6.4 retry) 404s and must never be used. Creating
    a combo is **unauthenticated on a default local gateway** (no API key
    needed); a 401 only appears if the gateway has `OMNIROUTE_API_KEY` set.
    Don't hand-curate the target list in the UI, don't add a second combo id,
    and don't mutate the preset from this path.
21. **The "Open gateway ↗" button opens the OmniRoute gateway's OWN web UI
    (providers/combos/keys/logs at `http://<host>:8080`), derived from the
    configured `base_url` via the pure `gatewayWebUrl(base_url)` helper.** The
    helper strips the trailing `/v1` API suffix (to land on the gateway UI root)
    and rewrites the container-side hostname (`host.docker.internal` /
    `localhost` / `127.0.0.1` / `0.0.0.0` — names the *browser* cannot resolve)
    to `window.location.hostname` (the host the user is browsing Agent Zero
    from), keeping the port. As of v2.6.6 it **never returns null / is never
    `:disabled`**: when no `base_url` is configured (or it's unparseable) it
    falls back to `http://<browser-host>:8080` (the gateway is published on the
    same host the user is browsing from). The button is also enlarged + given a
    distinct amber style (`.omni-gateway-btn`) so it's no longer the small grey
    button that read as broken. The root cause of the old grey/disabled button
    was two-fold: the store getter read `status.base_url` (always undefined — the
    status endpoint returns `configured_base_url`), AND the helper returned null
    with no fallback; both are fixed. The helper lives in **two** places —
    `webui/omniroute-store.js` (exported `gatewayWebUrl`, used by the Settings
    page's `openGateway()`) and `webui/dashboard.js` (module-local `function
    gatewayWebUrl`, used by `openGatewayDashboard()`) — duplicated on purpose
    because the dashboard uses its own Alpine scope and does not import the
    settings store (same reason `recoverGateway()`/`uninstallGateway()` are
    duplicated). Keep the two copies byte-for-byte in sync. Do NOT point this
    button at the plugin's internal `dashboard.html` — the dashboard content is
    now inlined on the Settings page (invariant #9); the gateway UI is a
    separate page the gateway itself serves on the same host:port as `/v1`.
    ⚠v2.6.7: the element is an `<a target="_blank" rel="noopener noreferrer">`
    bound directly to the gateway URL — NOT a `<button>` calling
    `openGateway()`/`openGatewayDashboard()` + `window.open()`. A synchronous
    `window.open()` from the click was being silently discarded by popup
    blockers (extensions / "block all popups" / lost user-gesture contexts):
    click, no tab, no console error. A real anchor navigation is never
    popup-blocked. The JS methods remain defined as fallbacks but no page wires
    them; the smoke tests pin the anchor contract on both pages.
    ⚠v2.6.8: in the Agent Zero DESKTOP APP this anchor points at the
    `localtest.me` LOOPBACK-ALIAS host (public DNS -> 127.0.0.1) instead of
    the loopback IP: the Launcher always denies new windows and silently
    drops loopback URLs in `openExternalIfSafe`, so a direct 127.0.0.1 URL
    can never open there; the alias passes the "remote instance" check and
    opens the user's system browser at the same local gateway. Never resolve
    the URL from a custom-scheme page origin's `window.location.hostname`
    (it is `"content"` in the desktop app) — see v2.6.8.
    ⚠v2.6.9: desktop-app detection is NOT protocol-only. Launcher >= v1.4
    loads instance WebUIs DIRECTLY over http (the `a0app://` scheme only
    serves the Launcher's *own* chrome), so on a v1.6.0 install the v2.6.8
    protocol check (`a0app:`) never fired and the alias was never applied —
    the button still silently dropped. `isDesktopApp` /
    `_isDesktopAppPage()` must sniff `/Electron\//` in `navigator.userAgent`
    too (in addition to the non-http(s) protocol check). Verified live:
    `example.com` link opens the browser, `127.0.0.1` link is dropped, so
    the UA sniff on an http origin is the only reliable signal available
    to page code.

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
  `api/dashboard.py`, `api/usage.py`, `api/combos.py` (v2.6.4 — provisions the
  `auto/utility-free` combo in the gateway from the user's live free models).
- **Utility-combo curator (pure, no I/O):** `helpers/utility_combo.py` — the single
  source of truth for which free models suit the utility slot (exclusions, tier
  ordering, slow-model reservation, cap). Unit-tested directly by
  `tests/smoke.py`; the gateway `POST /api/combos` call lives in
  `helpers/omniroute_client.py` (`create_combo` / `create_combo_async` /
  `gateway_root`).
- **Settings UI (Alpine store contract):** `webui/config.html` + `webui/omniroute-store.js`.
- **Dashboard page:** `webui/dashboard.html` + `webui/dashboard.js`. `openSettings()`
  opens the real plugin-settings panel via `pluginSettingsPrototype.openConfig('omniroute')`
  (NOT a hard `window.location.href` to standalone `config.html` — that page has no Alpine.js
  and no `config`/`context` injection, so it renders empty boxes + `$store.omnirouteStore
  undefined`). The Back button closes the modal via the framework `closeModal()` global (no
  SPA reload). See "v2.6.3" below.
- **WebUI injection points:** all four `extensions/webui/*` slots
  (`page-head/`, `chat-input-bottom-actions-end/`, `chat-top-end/`,
  `chat-input-bottom-actions-start/`, `sidebar-end/`) are **no-ops** kept only
  to keep the slots registered (prevents stale extension-point errors on
  reinstall/rollback). Since v2.6.5 the bottom chat-input pill is a no-op too
  — the online/offline status + the "open dashboard" / "open gateway ↗" links
  all live on the plugin pages (`webui/config.html` header row + the
  `webui/dashboard.html` actions row), not on the chat screen. Do not re-add a
  visible button to any extension slot.
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

## v2.6.3 — Dashboard Settings + Back fixed to be in-SPA (2026-08-08)

**Problem.** The dashboard's `openSettings()` did `window.location.href =
"/plugins/omniroute/webui/config.html"` — a hard navigation to a **standalone** config.html.
config.html uses Alpine (`x-data`, `$store.omnirouteStore`, `x-init`) but its `<head>` loads
only the store module, NOT Alpine.js, and the framework only injects the `config`/`context`
config.html binds to when it is loaded inside the `plugin-settings.html` wrapper (via
`<x-component path="/plugins/<name>/webui/config.html">`). On the standalone page none of
that is present → every Alpine binding inert → empty boxes + the `$store.omnirouteStore
undefined` error the user saw ("fail to load model … undefined" / "not even loading"). The
hard nav also reloaded the whole SPA, and the `<a href="/">Back</a>` link did the same —
the "it navigate the menu clicking on buttons" jank. There were also two always-visible
Settings entry points (header button + Mode-card "Change in settings →" link).

**Fix.**
- `openSettings()` (`webui/dashboard.js`) now calls
  `window.Alpine.store('pluginSettingsPrototype').openConfig('omniroute')` — the framework's
  canonical plugin-settings opener (the same one the plugin list uses). It opens
  `plugin-settings.html`, which injects `config`/`context` and loads config.html via
  `<x-component>` as a stacked modal on top of the dashboard modal. Closing it returns to the
  dashboard. The dashboard does not use the `pluginSettingsPrototype` store, so the store's
  internal `cleanup()` is safe to call from here. Defensive fallback: if the store is
  unavailable, toast an instruction instead of hard-navigating to a broken page.
- Back is now a `<button class="omni-back-btn" @click="closeModal()">` (framework global from
  `webui/js/modals.js`) — closes the modal, no SPA reload. (Falls back to `window.location
  href='/'` only if `closeModal` is somehow absent.)
- Removed the redundant Mode-card "Change in settings →" link. The header "Settings" button
  (always visible) remains the single always-visible entry point; the offline-empty-state
  "Settings" link stays (contextual, only when the gateway is offline — not a duplicate).
- Minor: `.omni-back-btn` style added to match the header action buttons.

**Stats counter note.** The dashboard API (`/api/plugins/omniroute/dashboard`,
`api/dashboard.py`) is unchanged and smoke-tested. Stats show 0/empty only when the gateway
is down OR when the user was looking at the broken standalone config page. After this fix
the Settings button no longer strands the user on that broken page.

**Display-name cleanup (also 2026-08-08).** User-facing prose that referenced the internal
codename `_model_fallback` now reads "a0 Model Fallback": the fallback-picker description in
`_model_config/webui/model-field.html` and the "Use as fallback" help text in
`webui/config.html`. Code identifiers, imports, paths, and developer docs (AGENTS.md / code
comments) intentionally still say `_model_fallback` — that is the real plugin name for
developers. The fallback picker rows were also redesigned to mirror the primary-model picker
(provider `<select>` on the left + model-name input with magnifier search in the middle); see
`_model_config/webui/model-field.html`.

## v2.6.4 — `auto/utility-free` utility route (2026-08-08)

> ⚠ **Corrections in v2.6.6 (see that section below).** This entry shipped
> with two empirically-wrong claims about the gateway combo API: (1) the combo
> name used a colon (`auto/utility:free`) — the gateway bans colons in names
> (400), so creation silently failed; renamed to `auto/utility-free`. (2) the
> idempotency retry was `PUT /api/combos/<name>`, but the gateway keys combos
> by its own UUID and ignores our id, so PUT-by-name 404s — replaced with
> upsert-by-name (GET find → PUT by UUID / POST). (3) "combo creation needs a
> key (401)" was a misdiagnosis of the colon-400; creation is unauthenticated
> on a default local gateway. The naming references below are updated to the
> canonical `auto/utility-free`; the superseded contract claims are marked
> ⚠v2.6.6 inline.

**Problem.** Agent Zero's **utility model** is the small/fast slot that fires on
nearly every turn (history/topic summarization, chat renaming, behaviour-ruleset
merging, memory memorize/recall/consolidation, document-query rewriting, email
dispatch, infection-safety auditing, optional chat compaction). Five of those jobs
emit JSON-as-text parsed leniently by `helpers/dirty_json.py` (no native
`json_mode`); context ranges from tiny single-turn prompts up to ~50k-char
history feeds; the per-call cascade timeout is 30 s. Users had no free route
tuned for this slot — the gateway ships `auto/coding:free` and
`auto/reasoning:free`, but nothing for the high-frequency utility workload, so
users either burned a paid model on every turn or hand-picked free models one by
one with no ordering and no filtering of non-text/toy models.

**Fix — a ready-made free route curated from the user's live models.**
- **`helpers/utility_combo.py`** (NEW, pure — no I/O) curates the target list for
  `auto/utility-free`:
  - **Excludes** non-utility ids: image (`flux`, `dall-e`, `imagen`, `seedream`,
    `kling`, `sora`, …), video (`veo`, `video`), audio (`whisper`, `tts`, `bark`,
    `playai`, `elevenlabs`, …), embeddings/rerank/moderation/clip (`embed`, `bge`,
    `e5-`, `gte-`, `rerank`, `llama-guard`, `clip`, …), tiny toy models that can't
    follow JSON (`0.5b`, `1.5b`, `135m`, `360m`, `smollm`, `tiny`), flaky low-rate
    no-auth providers (`g4f`, `dgrid/`), and deprecated providers (`galadriel`,
    `predibase`). Patterns are case-insensitive substrings matched against the
    bare gateway id (no `omniroute/` prefix).
  - **Orders** fast → mid → slow across 4 tiers (tier 0 = fast inference hosts
    `groq/`, `cerebras/`, `nvidia/`, `siliconflow/`, …; tier 1 = fast chat families
    `gemini*flash`, `gpt-oss`, `qwen3`, `gemma`, …; tier 2 = solid mid
    `deepseek-chat`, `mistral`, `glm-4`, …; tier 3 = best-but-slow reasoners
    `deepseek-r1`, `o1`/`o3`, `llama-*70b`, `qwen3-max`). Unknown-but-plausible
    chat models default to tier 2 (solid mid) — not dropped, not pinned first.
  - **Reserves `_RESERVE_SLOW = 2` tail slots** for tier-3 reasoners so the "best"
    models are always kept as last-resort fallbacks even when the fast/mid pool
    alone would fill the cap (per the user's "don't exclude the best models" rule —
    they're ordered last, not removed).
  - **Caps at `MAX_TARGETS = 12`**; original gateway order is preserved within
    each tier so re-runs with the same enabled providers produce a stable combo.
- **`helpers/omniroute_client.py`** (MODIFIED) adds the gateway-combos client:
  `create_combo` ⚠v2.6.6 (the v2.6.4 body did `POST {root}/api/combos` then
  retried as `PUT /api/combos/<urlencoded id>` on 409/400/"exist"/"duplicate" —
  but the gateway keys combos by its own UUID and ignores our `id`, so the
  PUT-by-name 404'd and never updated; v2.6.6 rewrites it as upsert-by-name:
  `GET /api/combos` → find by `name` → `PUT /api/combos/<UUID>` or `POST`),
  `list_combos`, `_find_combo_by_name` (v2.6.6), `_raw_request`
  (low-level urllib that returns status+body without raising, so conflict
  responses can be inspected), `gateway_root` (strips trailing `/v1` so the
  combos API at the gateway root is reachable), and `create_combo_async` /
  `list_combos_async` wrappers (per invariant #1/#2 — API handlers use the
  `*_async` wrappers, never the sync methods).
- **`api/combos.py`** (NEW) — `POST /api/plugins/omniroute/combos` handler. Glues
  the above together: `health_async` (one `GET /v1/models`) → filter
  `classify_tier == "free"` → `curate_utility_targets` → `create_combo_async` to
  the gateway. Returns `{ok, combo_id, selectable_as: "omniroute/auto/utility-free",
  strategy: "priority", target_count, targets:[…20], free_model_count,
  gateway_response:{status, method, body}, error}`. Side-effect-free w.r.t. the
  plugin folder: reads the live model list + POSTs one combo to the gateway; no
  preset / `config.json` / on-disk state touched.
- **`webui/dashboard.html` + `webui/dashboard.js`** (MODIFIED) — "Utility route"
  section with a "Create / refresh `auto/utility-free`" button calling
  `createUtilityCombo()` → the endpoint above. Result pill shows target count +
  sample ids; error path points the user at the gateway's own Combos dashboard.
  (v2.6.6: the same creator is also inlined on the Settings page — see v2.6.6.)

**Selectable as.** `omniroute/auto/utility-free` in the model picker (badged
"free" — the tier classifier already badges ids ending in `-free`/`:free` as
free, so `auto/utility-free` matches with no tier-code change). The user picks
it for the **Utility Model** slot in Settings → Model Presets; this path never
writes the preset.

**Idempotency.** ⚠v2.6.6 — the v2.6.4 claim here ("retries `POST` as `PUT` on a
conflict") was wrong: `PUT /api/combos/<name>` 404s because the gateway keys by
UUID. The real idempotent pattern is upsert-by-name (`GET /api/combos` → find by
`name` → `PUT /api/combos/<UUID>` to refresh, else `POST`). Clicking "Create /
refresh" again updates the existing combo in place, so the combo always reflects
the user's current live free models.

**Auth asymmetry — ⚠v2.6.6 corrected.** The v2.6.4 claim that "combo *creation*
needs a key (401 without one)" was a misdiagnosis: the actual failure was a
colon-in-name 400, misread as a 401. On a **default local gateway**, both
`GET /v1/models` (listing free models) and `POST /api/combos` (creating a combo)
are **unauthenticated** — no key needed. A `401 Authentication required` only
appears if the gateway has been configured to require an admin token
(`OMNIROUTE_API_KEY` set on the gateway); in that case `api/combos.py:
_combo_create_error` detects the 401 and returns a message telling the user to
set the OmniRoute API key in Settings (the gateway's control token, shown in the
gateway UI at the host URL). No-key users can also pick an existing free combo
the gateway already ships (`auto/best-free`, `auto/coding:free`) for the Utility
slot without creating anything.

**Smoke coverage.** `tests/smoke.py` adds tests for the curator (exclusions,
fast→slow ordering, cap + slow reservation), the pure `gateway_root` helper,
`create_combo`'s POST→PUT retry, the `api/combos.py` endpoint's full
curate-from-live-free-models glue (via the path-aware `StubServer`), and the
401-specific error message.

## v2.6.5 — gateway dashboard link + bottom pill removed (2026-08-08)

**Problem.** Two related complaints from the user: (1) they had saved their
OmniRoute API key but had *never seen* the gateway's own "nice dashboard from
localhost" — the bottom "OmniRoute" pill (the only chat-screen entry point)
opened the plugin's *internal* dashboard modal, **not** the gateway's own web
UI at `http://localhost:8080`; nothing in the plugin linked there. (2) that
same bottom pill was redundant — it duplicated the online/offline status
already shown on the plugin's Settings page and dashboard modal, and clutters
the chat input.

**Fix.**
- **Removed the bottom pill.** `extensions/webui/chat-input-bottom-actions-end/
  omniroute-button.html` is now a no-op (renders nothing, stops the 60s
  `/status` polling). The file is kept so the extension slot stays registered.
  Its two functions were relocated onto the plugin pages:
    - *online/offline status* — already shown on `config.html` + `dashboard.html`
      (the pages poll on demand when opened, which is what the user asked for:
      "when I want to open it up, then I can see whether OmniRoute is online or
      offline").
    - *"open the dashboard"* — a new **"Open dashboard"** button on
      `config.html` calls `openModal('/plugins/omniroute/webui/dashboard.html')`,
      so the model browser + the `auto/utility-free` creator stay reachable now
      that the pill is gone. ⚠v2.6.6: this "Open dashboard" button was then
      *removed* from `config.html` — the dashboard content (counts, tier bar, the
      `auto/utility-free` creator) is now shown INLINE on the Settings page, so
      the extra click is gone. The standalone `dashboard.html` modal is kept.
- **Added an "Open gateway ↗" button to BOTH pages** (`config.html` header row
  + `dashboard.html` actions row). It opens the OmniRoute gateway's OWN web UI
  (providers, combos, API keys, logs) at `http://<host>:8080` in a new tab — the
  page the pill never linked to. The URL is derived from the configured
  `base_url` by the pure `gatewayWebUrl(base_url)` helper, which strips the
  `/v1` API suffix and rewrites the container-side hostname
  (`host.docker.internal`/`localhost`/`127.0.0.1`/`0.0.0.0`) to
  `window.location.hostname` (the browser can't resolve the container-side
  names; the gateway is published on the same host the user is browsing from),
  keeping the port. `null` on missing/unparseable input → the button is
  `:disabled` and shows a "not configured" tooltip, never a blank tab.
  ⚠v2.6.6: the `null`/`:disabled` behavior was removed — the helper now falls
  back to `http://<browser-host>:8080` when no `base_url` is configured, so the
  button is always enabled (and enlarged). See v2.6.6.
- The helper is **duplicated** in `webui/omniroute-store.js` (exported, used by
  `$store.omnirouteStore.openGateway()` on the Settings page) and
  `webui/dashboard.js` (module-local, used by `openGatewayDashboard()` on the
  dashboard modal) — the dashboard uses its own Alpine scope and does not
  import the settings store (same reason `recoverGateway()` /
  `uninstallGateway()` are duplicated). The two copies must stay in sync.

**Why two buttons, not one.** "Open dashboard" opens the plugin's *internal*
model browser (your live models, tiers, the `auto/utility-free` creator) as an
in-SPA modal. "Open gateway ↗" opens the *gateway's own* admin page in a new
browser tab. They are different pages serving different purposes; conflating
them (as the old pill did) is exactly what hid the gateway UI from the user.
The `↗` glyph marks the new-tab/external one. ⚠v2.6.6: on the Settings page
there is now only ONE button ("Open gateway ↗") — the "Open dashboard" button
was removed because the dashboard content is inlined directly on the page.
The standalone `dashboard.html` modal keeps its own "Open gateway ↗" button.

**Files.** `plugin.yaml` (2.6.4 → 2.6.5 + description mentions the gateway
link), `hooks.py` + `execute.py` (EXPECTED_VERSION → 2.6.5), the no-op bottom
button, `webui/config.html` + `webui/dashboard.html` (the buttons),
`webui/omniroute-store.js` + `webui/dashboard.js` (the helper + methods).

**Smoke coverage.** `test_bottom_button_has_live_status_pill` is replaced by
`test_bottom_button_is_noop_after_v265` (asserts the no-op contract). New
tests pin both buttons on both pages, the store + dashboard helpers, and the
pure `gatewayWebUrl` transforms (container-side hostname rewrite, `/v1`
stripping, port preservation, `null` on bad input) via a Node harness that
loads the store as a classic script with `createStore` stubbed. ⚠v2.6.6: the
`null`-on-bad-input assertion was replaced — the helper now returns
`http://<browser-host>:8080` for empty/unparseable input (never null).

**Auth note still applies.** The "Open gateway ↗" button lands the user on the
gateway UI where they can grab the control token / manage combos; but creating
the `auto/utility-free` combo still needs that API key in the plugin's
Settings (see the v2.6.4 "Auth asymmetry" note above). ⚠v2.6.6: this sentence
is superseded — combo creation is unauthenticated on a default local gateway;
the 401 only appears if the gateway has `OMNIROUTE_API_KEY` set. See v2.6.6.

## v2.6.6 — inline dashboard, fixed gateway button, working utility route (2026-08-10)

Four user-reported issues, all fixed in one pass. The root cause of #1/#2 was a
store getter reading the wrong status field; the root cause of #3/#4 was a
colon in the combo name (gateway 400) plus a wrong idempotency retry
(PUT-by-name 404). All four were validated against the live gateway.

**#1 — Dashboard inlined onto the Settings page.** The user did not want to
click a separate "Open dashboard" button to see the live state. `webui/config.html`
now renders the dashboard INLINE: a quick-stats row (model count + tier split +
latency + base URL), a tier-breakdown bar (free/cheap/key/sub proportions), and
the `auto/utility-free` creator — all bound to `$store.omnirouteStore` getters
(`dashModelCount`, `dashFreeCount`, `pctFree`, …) populated by `loadDashboard()`
(POST `/api/plugins/omniroute/dashboard`, the same endpoint the standalone
dashboard uses). The "Open dashboard" button on `config.html` is REMOVED; the
standalone `dashboard.html` modal is kept (invariant #9). New CSS: `.omni-stat-grid`,
`.omni-stat-card`, `.omni-tier-bar`, `.omni-tier-legend`.

**#2 — "Open gateway ↗" button enlarged + fixed (was grey/disabled/non-functional).**
Two bugs: (a) the store `gatewayWebUrl` getter read `status.base_url`, which is
always `undefined` — the status endpoint returns `configured_base_url` (see
`api/status.py`). Fixed to read `status.configured_base_url || status.base_url`.
(b) the `gatewayWebUrl(base_url)` helper returned `null` for a missing
`base_url`, and the button was `:disabled` on `null` — so before the first
refresh populated status (or when the injected settings had no `base_url` for
the current scope) the button stayed permanently grey. Fixed: the helper now
falls back to `http://<browser-host>:8080` (`DEFAULT_GATEWAY_PORT`) when no
`base_url` is available, so it NEVER returns null and the button is NEVER
disabled. The button is also enlarged + restyled (`.omni-gateway-btn`, amber
gradient) so it reads as a primary action, not a broken grey stub. The same
helper fix is mirrored in `webui/dashboard.js` (the duplicated copy — invariant #21).

**#3 — `auto/utility-free` selectable in the model picker.** The combo was
never actually created in the gateway, so `omniroute/auto/utility-free` never
appeared in `/v1/models` and the picker couldn't list it. Root cause: the combo
NAME contained a colon (`auto/utility:free`), and the gateway bans colons in
names (400 "Name can only contain letters, numbers, spaces, -, _, /, ., [ and
]"). Renamed to `auto/utility-free` (colon-free; slash preserved in the
slugified model id). The tier classifier already badges ids ending in `-free`
as "free" (`_TIER_FREE_PATTERNS` includes `r"-free"`), so `auto/utility-free`
is badged free with no classifier change. The built-in `auto/coding:free`,
`auto/coding:fast`, `auto/best-free` already exist in `/v1/models` and ARE
selectable as `omniroute/auto/coding:free` etc. — the picker does a live `GET`
with no colon filtering, so they were always available; the user couldn't find
them because the picker filters by the current model-name query.

**#4 — Auto-detect new free models.** `createUtilityCombo()` now runs
automatically once on Settings-page load (after the first `refresh()` resolves
and `installState === "ready"`), and the standalone dashboard's `init()` auto-
calls it after the first `forceRefresh()`. Because `create_combo` is idempotent
(upsert-by-name), re-running it re-curates from the user's CURRENT live free
models — so newly-enabled providers are picked up with no manual curation. The
curator (`helpers/utility_combo.py`) added `r"^auto/"` to `_EXCLUDE_PATTERNS`
so the combo never targets itself or another `auto/*` meta-route (those are
classified "free" via the `-free`/`:free` pattern and would otherwise be curated
into their own target list → recursive routing).

**Verified gateway combo API contract (empirical, 2026-08-10).** Documented
here once so it is not re-derived. `POST /api/combos` with body
`{"name","strategy","models":[{"model":"<id>"}]}` (the field is `models`, NOT
`targets` — `targets` is ignored and silently creates an empty combo). Colons
are BANNED in `name` (400); slashes are preserved. The gateway assigns its own
UUID `id` and ignores any `id`/`slug` we send; the combo's model id in
`/v1/models` is the slugified `name`. Duplicate-name `POST` is rejected (no
duplicate created). `PUT /api/combos/<UUID>` updates in place; `PUT /api/combos/
<name>` 404s (keyed by UUID). `GET /api/combos` returns `{"combos":[{id,name,
models,…}]}`. `DELETE /api/combos/<UUID>` works. No auth on a default local
gateway; 401 only if `OMNIROUTE_API_KEY` is set. The correct idempotent pattern
is **upsert by name**: `GET /api/combos` → find by `name` → `PUT /api/combos/
<UUID>` or `POST`. This is implemented in `helpers/omniroute_client.py:
create_combo` (+ `_find_combo_by_name`).

**Files.** `plugin.yaml` (2.6.5 → 2.6.6 + description), `hooks.py` + `execute.py`
(EXPECTED_VERSION → 2.6.6), `helpers/omniroute_client.py` (`create_combo`
rewritten + `_find_combo_by_name`), `helpers/utility_combo.py` (`COMBO_ID`
rename + `^auto/` exclusion), `api/combos.py` (rename + 401 docstring), 
`webui/config.html` (inline dashboard + enlarged gateway button + CSS),
`webui/omniroute-store.js` (dashboard state + `loadDashboard` +
`createUtilityCombo` + getters + `gatewayWebUrl` fix + auto-refresh),
`webui/dashboard.js` (mirror `gatewayWebUrl` fix + rename + auto-combo in
`init`), `webui/dashboard.html` (rename), `README.md` + `AGENTS.md` (rename +
contract corrections + this section), `tests/smoke.py` (combo tests updated
for the upsert-by-name flow + rename).

## v2.6.7 — "Open gateway ↗" is now an `<a target="_blank">`, not window.open (2026-08-31)

**Problem.** User report: on the Settings page the "Open gateway ↗" button did
nothing — click, no tab, no error. The URL derivation was already correct
(v2.6.6 fix: the gateway is published on the Docker host at `:8080`, and the
derived `http://<browser-host>:8080` was reachable — verified while debugging:
`docker ps` shows the gateway published on `0.0.0.0:8080`/`[::]:8080`). The
remaining way a synchronous `window.open()` from a real click fails *silently*
with no tab and no error is popup blocking: popup-blocker extensions, a
"Block all popups" browser setting, or a lost user-gesture context (an
intermediary `await` between the click and the open, or the Alpine/x-component
expression evaluation path) all make the browser discard the call without any
console error.

**Fix.** The button element is now a real anchor navigation, which browsers
never popup-block:

- `webui/config.html` — `<a class="omni-gateway-btn" :href=
  "$store.omnirouteStore.gatewayWebUrl" target="_blank" rel="noopener
  noreferrer">`. `gatewayWebUrl` always resolves (v2.6.6 fallback), so the
  href is always present. CSS adds `display:inline-block` + `text-decoration:
  none` so the anchor looks identical to the old button. The `@click` handler
  and the store `openGateway()` call are gone from the page (the method stays
  defined in `omniroute-store.js` as a JS fallback and is still covered by
  `test_store_exposes_gateway_helpers`).
- `webui/dashboard.html` — same change: `<a class="omni-gateway-link"
  :href="gatewayUrl" target="_blank" rel="noopener noreferrer" x-show=
  "gatewayUrl">` styled to match the neighboring buttons (`.omni-gateway-link`).
  `x-show` (not `:disabled`) because anchors don't support disabled; the
  anchor only renders once `gatewayUrl` resolves. `openGatewayDashboard()`
  stays in `dashboard.js` as the JS fallback.
- `tests/smoke.py` — `test_config_html_has_open_gateway_button` and
  `test_dashboard_html_has_open_gateway_button` now pin the ANCHOR contract
  (`target="_blank"` + `rel="noopener noreferrer"` + bound href) so the
  window.open path doesn't creep back. The store/dashboard.js method tests
  are unchanged (the JS methods remain).

**Keep in sync.** The anchor is in BOTH `config.html` and `dashboard.html` —
if one is ever reverted to a `<button>`+`window.open`, both smoke tests fail.

**Files.** `plugin.yaml` (2.6.6 → 2.6.7), `webui/config.html` (anchor + CSS),
`webui/dashboard.html` (anchor + CSS), `tests/smoke.py` (anchor contract),
`AGENTS.md` (this section + invariant #21).

## v2.6.8 — desktop app: loopback-alias gateway URL + hostname fix (2026-08-31)

**Problem.** User report (desktop app — the A0 Launcher, Electron, custom
`a0app://` origin): "Open gateway ↗" still does nothing, AND the plugin
settings modal took ~1 minute / several clicks to open. Two stacked desktop-
app bugs, discovered by reading the Launcher's window-open policy
(`shell/main.js` + `shell/instance_tabs.js`, public repo agent0ai/a0-launcher):

1. **Hostname bug.** On the custom-scheme page origin, `window.location.
   hostname` is the protocol path component (`"content"`) — the old
   `gatewayWebUrl` helper produced `http://content:8080`, an unresolvable
   address. (In a real browser the helper is fine.)
2. **Launcher policy.** `setWindowOpenHandler` ALWAYS denies new windows:
   in-tab navigation only happens for SAME-origin URLs, everything else goes
   to `openExternalIfSafe(url)` — which SILENTLY DROPS loopback URLs (its
   allowlist check `isAllowedLocalInstanceUrl` matches only
   localhost/127.0.0.1/[::1]/::1 and returns "not opened"). So no
   `window.open`/`<a target=_blank>`/anchor can ever open
   `http://127.0.0.1:8080` in the desktop app — the link is dropped with no
   error. Embedding is also impossible: the gateway sends
   `X-Frame-Options: DENY` + `CSP frame-ancestors 'none'`, and it is a
   Next.js SPA with absolute `/_next` + `/api` paths, so same-origin proxying
   through A0 would collide with A0's own `/api` namespace.

**Fix.** On a non-http(s) page origin (desktop app), the helper now (a) falls
back to the LOOPBACK host instead of `"content"`, and (b) with
`loopbackAlias: true`, swaps every loopback hostname for the public
wildcard-DNS loopback alias **`localtest.me`** (a public A record →
`127.0.0.1`). The alias hostname is NOT in the Launcher's loopback allowlist,
so it passes the "remote instance" check → `shell.openExternal` → **the
USER'S SYSTEM BROWSER opens**, resolves the alias to 127.0.0.1 via (cached)
public DNS, and lands on the same local gateway. This matches the user's
explicit fallback request ("open up my browser instead" — in-app embedding of
the gateway UI is technically impossible under the launcher policy + gateway
framing headers).

- `webui/omniroute-store.js` — `_browserGatewayHost(fallback)` (protocol-
  aware hostname), `_LOOPBACK_HOSTS`, `_LOOPBACK_ALIAS_HOST = "localtest.me"`,
  `gatewayWebUrl(base_url, { loopbackAlias })` (2nd optional arg, default
  off → legacy behavior preserved byte-for-byte), `isDesktopApp` getter
  (page protocol is not http/https), `gatewayWebUrl` getter now passes
  `{ loopbackAlias: this.isDesktopApp }`.
- `webui/dashboard.js` — same changes mirrored (its own scope duplicate;
  invariant #21), `gatewayUrl` getter passes `{ loopbackAlias:
  _isDesktopAppPage() }`.
- `tests/smoke.py` — legacy transform harness unchanged (proves default
  behavior); NEW `test_gateway_web_url_desktop_app_alias` harness with an
  `a0app://content/` window pins the desktop-app transforms (no `'content'`
  host leak, alias only with the option, LAN IPs untouched).
- In a NORMAL browser nothing changes: direct `http://<browser-host>:8080`
  anchor, new tab.

**Caveats (documented, deliberate).** The alias needs internet DNS for
`localtest.me` (cached after first lookup). A fully-offline desktop app
cannot open the gateway UI by ANY in-repo mechanism (launcher drops loopback
URLs by policy) — the settings page still displays the direct base URL and
the tooltip shows the exact URL that will open. The alias is surfaced to the
user in the button tooltip; if they prefer no third-party DNS, the upstream
fix is in the Launcher (`openExternalIfSafe` dropping loopback URLs) — file
an issue on agent0ai/a0-launcher if needed.

**Files.** `plugin.yaml` (2.6.7 → 2.6.8), `hooks.py` + `execute.py`
(EXPECTED_VERSION → 2.6.8), `webui/omniroute-store.js` + `webui/dashboard.js`
(hostname fix + alias), `tests/smoke.py` (desktop harness), `AGENTS.md`
(invariant #21 + this section).

## v2.6.9 — desktop app detection: Electron UA sniff (2026-08-31)

**Problem.** v2.6.8 shipped, user restarted container + app, still nothing.
Live diagnosis via the debug links added to `config.html`: `example.com`
(target=_blank) opened the system browser, but the gateway link did nothing
and the tooltip showed `http://127.0.0.1:8080` — i.e. the v2.6.8
`loopbackAlias` option was never applied. Root cause: **desktop-app
detection was protocol-only** (`window.location.protocol` not http/https =
"a0app:"), but the INSTALLED Launcher (v1.6.0) loads instance WebUIs
DIRECTLY over http — the `a0app://` scheme only serves the Launcher's own
chrome, not instance pages. Page code cannot distinguish the app webview
from a normal browser tab by origin.

**Fix.** `isDesktopApp` (omniroute-store.js) / `_isDesktopAppPage()`
(dashboard.js) additionally sniff `/Electron\//` in `navigator.userAgent` —
Electron webviews always advertise it, regular browsers never do. The alias
now fires on the launcher's http origin; clicking still routes through
`setWindowOpenHandler` → `openExternalIfSafe` → system browser
(`instance_tabs.js:58 parseHttpUrl` accepts http(s) only, but that filters
raw strings pre-alias — the alias URL is plain http and passes).

**Also in this release.** `webui/config.html` gained a visible `v2.6.9`
freshness marker next to the button (the desktop app has no devtools; the
marker tells stale from fresh in one glance — keep it updated with each
version). Two TEMP diagnostic links (`debug: open example.com` +
`debug:<resolved URL>`) were used to bisect where the click died
(confirmation: example.com opened, loopback URL dropped) and were removed
after the user confirmed the alias fix works.

**Files.** `webui/omniroute-store.js` + `webui/dashboard.js` (UA sniff),
`webui/config.html` (marker + debug links), `tests/smoke.py`
(`test_desktop_app_detected_via_electron_ua`), `plugin.yaml` (2.6.8 →
2.6.9), `hooks.py` + `execute.py` (EXPECTED_VERSION → 2.6.9), `AGENTS.md`
(invariant #21 + this section).
