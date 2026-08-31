// OmniRoute plugin - Alpine store (v1.3.0 — install wizard).
// Implements the v2.2 plugin-settings-store contract plus:
//   - installState (not_installed | installing | ready) drives the UI
//   - installScript / copyToClipboard for the install wizard
//   - autodetect via the backend OmniRouteClient
//
// Note (v1.3.0): the previous "one-liner" copy button (a single
// `irm https://raw.githubusercontent.com/agent0ai-community/... | iex`
// command) was removed because the public repo it pointed at did not
// exist (404 on the raw.githubusercontent.com URL). The install is
// now only available via the in-page "Show full install script"
// advanced view, which fetches the script from the local plugin
// asset server (no public URL dependency).

import { createStore } from "/js/AlpineStore.js";
import { toastFrontendError, toastFrontendSuccess, toastFrontendInfo } from "/components/notifications/notification-store.js";

// ---- gateway web-URL helper (v2.6.5) -----------------------------------
// Turn the plugin's configured ``base_url`` (the container-perspective
// OpenAI API endpoint, e.g. ``http://host.docker.internal:8080/v1``) into
// a URL the BROWSER can actually open to reach the OmniRoute gateway's own
// web UI (the providers/combos/keys/logs admin page).
//
// Two transforms:
//   1. Strip the trailing ``/v1`` API suffix (and any trailing slash) so we
//      land on the gateway root, where the gateway serves its own web UI.
//   2. Replace container-side hostnames the browser cannot resolve —
//      ``host.docker.internal`` is a Docker-Desktop container-only DNS
//      name; ``localhost`` / ``127.0.0.1`` inside the container refer to the
//      container itself, not the host. The browser is reaching Agent Zero
//      on some host, so reuse ``window.location.hostname`` (the host the
//      user is currently browsing from). The gateway is published on the
//      same host (the installer maps host :8080 -> container :20128), so
//      <browser-host>:<base_url-port> is the right address. Keep the port
//      from base_url; if it was the scheme default, URL omits it (correct).
//
// Returns ``null`` only when no URL can be derived at all. As of v2.6.6 the
// gateway is published on the Docker host at a known port (default 8080), and
// the browser is already browsing that host, so when ``base_url`` is missing
// we fall back to ``http://<browser-host>:<DEFAULT_GATEWAY_PORT>`` instead of
// returning null. This keeps the "Open gateway" button enabled and functional
// even before the first status refresh populates ``status.base_url`` — which
// was the root cause of the permanently grey/disabled button (the status
// endpoint returns ``configured_base_url``, not ``base_url``; see the getter
// below).
//
// Duplicated (intentionally) in webui/dashboard.js — the dashboard uses its
// own Alpine scope and does not import the settings store, mirroring the
// existing recoverGateway()/uninstallGateway() duplication. Keep the two
// copies in sync.
const DEFAULT_GATEWAY_PORT = "8080";

// Hostname spellings that all reach the local machine's loopback interface.
const _LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"]);

// v2.6.8: public wildcard-DNS loopback alias. ``localtest.me`` (and every
// subdomain of it) has a public A record -> 127.0.0.1, so browsing it on the
// gateway's port reaches the SAME local gateway as 127.0.0.1:<port> — but as
// far as URL PARSING goes it is an ordinary internet hostname. This matters
// for the Agent Zero DESKTOP APP (A0 Launcher, Electron):
//   * its window-open policy only opens URLs in the system browser when they
//     pass the "remote instance" check, and that check SILENTLY DROPS local
//     URLs (hostname in {localhost,127.0.0.1,[::1],::1}) — a link straight to
//     http://127.0.0.1:8080 can never open there;
//   * the alias hostname is not in that list, so the launcher treats it as a
//     remote URL and calls shell.openExternal -> the USER'S SYSTEM BROWSER
//     opens (per their explicit request), resolves the alias to 127.0.0.1 via
//     public DNS, and lands on the same local gateway.
// The alias needs internet access for (cached) DNS; a fully-offline desktop
// app cannot open the gateway UI at all (launcher policy) — the settings page
// also displays the direct URL so users can copy it manually.
const _LOOPBACK_ALIAS_HOST = "localtest.me";

// v2.6.8: hostname the BROWSER should use to reach the gateway. Desktop app
// pages are served under a custom scheme (``a0app://content/...``) whose
// "hostname" is a protocol path component, not a resolvable host — the old
// code blindly used it and produced ``http://content:8080`` (unresolvable —
// the desktop-app half of the "Open gateway" button did nothing). On any
// non-http(s) page origin the gateway is reachable at loopback (the gateway
// is published on the Docker HOST = the machine the desktop app runs on).
function _browserGatewayHost(fallback) {
  if (typeof window === "undefined" || !window.location) return fallback;
  const proto = window.location.protocol;
  if (proto && proto !== "http:" && proto !== "https:") return fallback;
  return window.location.hostname || fallback;
}

function _aliasIfNeeded(host, options) {
  if (options && options.loopbackAlias && _LOOPBACK_HOSTS.has(host)) {
    return _LOOPBACK_ALIAS_HOST;
  }
  return host;
}

export function gatewayWebUrl(base_url, options) {
  const raw = (base_url || "").trim();
  // No configured base_url: derive the gateway web URL from the host the
  // browser is already on + the default gateway port. The gateway's web UI
  // is served at the root (http://<host>:8080/ -> 307 -> /dashboard), so
  // landing on the root is correct.
  if (!raw) {
    const host = _aliasIfNeeded(_browserGatewayHost("localhost"), options);
    return `http://${host}:${DEFAULT_GATEWAY_PORT}`;
  }
  try {
    const u = new URL(raw);
    let host = u.hostname;
    if (host === "host.docker.internal" || _LOOPBACK_HOSTS.has(host)) {
      // Container-side names the browser cannot resolve -> the browsing
      // machine's own host (or loopback in the desktop app).
      host = _browserGatewayHost("localhost");
    }
    host = _aliasIfNeeded(host, options);
    const port = u.port ? ":" + u.port : "";
    return `${u.protocol}//${host}${port}`;
  } catch (e) {
    const host = _aliasIfNeeded(_browserGatewayHost("localhost"), options);
    return `http://${host}:${DEFAULT_GATEWAY_PORT}`;
  }
}

export const store = createStore("omnirouteStore", {
  // ---- state ----
  status: null,
  models: [],
  busy: false,
  testResult: null,
  installScript: "",
  recovering: false,
  uninstalling: false,
  _config: null,
  _context: null,
  _pollTimer: null,
  _recoverRefreshTimer: null,
  _uninstallRefreshTimer: null,

  // ---- v2.6.6: inline-dashboard state (shown directly on the settings page) ----
  // Populated by loadDashboard() (POST /api/plugins/omniroute/dashboard) and
  // createUtilityCombo() (POST /api/plugins/omniroute/combos). Mirrors the
  // shape the standalone dashboard.js uses, so the same /dashboard endpoint
  // backs both surfaces. Null/empty until first load.
  dash: null,                 // full dashboard payload (provider_count, models, ...)
  dashBusy: false,
  dashError: null,
  utilityComboBusy: false,
  utilityComboResult: null,   // {ok, count, sample, method, freeCount} | {ok:false, error}

  // ---- computed labels (used by the template) ----
  get installState() {
    if (this.status && this.status.reachable) return "ready";
    if (this.busy) return "installing";
    return "not_installed";
  },
  get statusLabel() {
    if (this.installState === "ready") {
      const n = this.status?.provider_count ?? 0;
      const ms = this.status?.latency_ms ?? 0;
      return `Online · ${n} models · ${ms} ms`;
    }
    if (this.installState === "installing") return "Checking...";
    if (this.recovering) return "Recovering…";
    if (this.uninstalling) return "Removing…";
    return "Not installed";
  },

  // ---- v2.6.8: are we running inside the Agent Zero DESKTOP APP? ----
  // The A0 Launcher (Electron) serves instance UIs over real http(s) when
  // browsing normally, but its own chrome uses the custom ``a0app://``
  // scheme. On any non-http(s) page origin the page hostname is meaningless
  // AND the launcher's window-open policy silently drops loopback URLs — both
  // of which made the old "Open gateway" button a no-op in the desktop app.
  get isDesktopApp() {
    try {
      if (typeof window === "undefined" || !window.location) return false;
      // v2.6.9: detect the A0 Launcher (Electron) by user agent as well —
      // Launcher >= v1.4 loads instance WebUIs DIRECTLY over http
      // (window.location is a plain http origin), so the old protocol-only
      // check (``a0app:``) never fired there and the ``localtest.me``
      // loopback alias was never applied -> button still dropped.
      if (/Electron\//.test(navigator.userAgent || "")) return true;
      return (
        window.location.protocol !== "http:" &&
        window.location.protocol !== "https:"
      );
    } catch (e) {
      return false;
    }
  },

  // ---- v2.6.5: gateway web-UI URL (browser-reachable) ----
  // Reads the configured base_url (or the last probed status base_url) and
  // rewrites it to the gateway's own web UI root for the current browser
  // host. See the module-level gatewayWebUrl() helper for the transforms.
  //
  // v2.6.6: the status endpoint returns ``configured_base_url`` (NOT
  // ``base_url``) — the old getter read ``status.base_url`` which was always
  // undefined, so after a refresh the button still relied solely on
  // ``_config.base_url`` and stayed grey when the injected settings had no
  // base_url for the current scope. We now read ``configured_base_url`` too,
  // and the module-level helper falls back to ``<browser-host>:8080`` when no
  // base_url is available anywhere, so the button is never grey.
  //
  // v2.6.8: inside the desktop app the URL uses the ``localtest.me``
  // loopback-ALIAS host (public DNS -> 127.0.0.1): the launcher refuses to
  // open loopback URLs (silent drop), but opens alias-hostname URLs in the
  // user's system browser, which then reaches the same local gateway. In a
  // normal browser the direct loopback/LAN URL is returned unchanged.
  get gatewayWebUrl() {
    const base =
      (this._config && this._config.base_url) ||
      (this.status && (this.status.configured_base_url || this.status.base_url)) ||
      "";
    return gatewayWebUrl(base, { loopbackAlias: this.isDesktopApp });
  },

  // Open the OmniRoute gateway's own web UI (providers, combos, API keys,
  // logs) in a new browser tab. This is the page the bottom pill NEVER
  // linked to (it opened the plugin's internal dashboard instead), which
  // is why users never found the "nice dashboard from localhost". Guarded
  // against a missing/unparseable base_url so we never open a blank tab.
  openGateway() {
    const url = this.gatewayWebUrl;
    if (!url) {
      toastFrontendError(
        "Gateway URL is not configured. Set the OmniRoute base URL in Advanced settings.",
        "OmniRoute"
      );
      return;
    }
    window.open(url, "_blank", "noopener");
  },

  // ---- v2.2 plugin-settings-store contract ----
  async init(config, context) {
    this._config = config || {};
    this._context = context || null;
    // Lazy-load the full install script for the advanced view
    try {
      const r = await fetch("/plugins/omniroute/webui/install-omniroute.ps1");
      if (r.ok) this.installScript = await r.text();
    } catch (e) {
      this.installScript = "# (could not load install-omniroute.ps1 from plugin assets)";
    }
    // v2.6.6: load the inline dashboard + auto-refresh the auto/utility-free
    // combo as soon as we know the gateway is up, so the settings page shows
    // live state immediately and the combo picks up any newly-enabled free
    // providers without the user clicking anything (#1 + #4).
    this.refresh().then(() => {
      this.loadDashboard();
      if (this.installState === "ready") this.createUtilityCombo();
    });
  },

  bindConfig(config) {
    this._config = config || this._config;
  },

  cleanup() {
    if (this._pollTimer) {
      clearTimeout(this._pollTimer);
      this._pollTimer = null;
    }
    if (this._recoverRefreshTimer) {
      clearTimeout(this._recoverRefreshTimer);
      this._recoverRefreshTimer = null;
    }
    if (this._uninstallRefreshTimer) {
      clearTimeout(this._uninstallRefreshTimer);
      this._uninstallRefreshTimer = null;
    }
    this._config = null;
    this._context = null;
    this.status = null;
    this.models = [];
    this.testResult = null;
    this._lastModelsResponse = null;
    this.recovering = false;
    this.uninstalling = false;
    this.dash = null;
    this.dashBusy = false;
    this.dashError = null;
    this.utilityComboBusy = false;
    this.utilityComboResult = null;
  },

  // ---- actions ----
  async refresh() {
    this.busy = true;
    try {
      const r = await fetch("/api/plugins/omniroute/status", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      this.status = await r.json();
    } catch (e) {
      this.status = { reachable: false, error: String(e), provider_count: 0 };
    } finally {
      this.busy = false;
    }
  },

  async loadModels() {
    this.busy = true;
    try {
      const r = await fetch("/api/plugins/omniroute/models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filter: "" }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      // Record the raw response for the diagnostics panel — helps
      // distinguish "API returned 0 items" from "API returned an
      // unexpected shape" (the previous bug rendered [object Object]
      // for every row, which the diagnostics surface would have caught).
      this._lastModelsResponse = data;
      this.models = data.filtered || data.models || [];
    } catch (e) {
      this._lastModelsResponse = { error: String(e) };
      toastFrontendError(String(e), "OmniRoute");
    } finally {
      this.busy = false;
    }
  },

  // ---- v2.6.6: inline dashboard (Settings page) ----
  // Fetches the rich dashboard payload (tier counts + model list + latency)
  // from the same /api/plugins/omniroute/dashboard endpoint the standalone
  // dashboard modal uses, so the user sees the live gateway state directly on
  // the plugin settings page without clicking "Open dashboard".
  async loadDashboard() {
    this.dashBusy = true;
    this.dashError = null;
    try {
      const r = await fetch("/api/plugins/omniroute/dashboard", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      this.dash = await r.json();
    } catch (e) {
      this.dashError = String(e);
      this.dash = null;
    } finally {
      this.dashBusy = false;
    }
  },

  // Create / refresh the auto/utility-free combo IN THE GATEWAY from the user's
  // live free models. The backend (api/combos.py) curates the target list and
  // upserts the combo by name (POST then PUT-by-UUID). After this succeeds,
  // omniroute/auto/utility-free appears in the model picker and is selectable
  // for the Utility slot. Idempotent (PUT refreshes an existing combo), so
  // calling it on every settings-page load auto-picks-up newly-enabled free
  // providers — the "auto-detect new free models" behavior the user asked for.
  async createUtilityCombo() {
    if (this.utilityComboBusy) return;
    this.utilityComboBusy = true;
    this.utilityComboResult = null;
    try {
      const r = await fetch("/api/plugins/omniroute/combos", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      let d = null;
      try { d = await r.json(); } catch (e) { /* non-JSON error body */ }
      if (!r.ok || !d || !d.ok) {
        const msg = (d && (d.error || d.gateway_response)) || ("HTTP " + r.status);
        this.utilityComboResult = { ok: false, error: String(msg) };
        return;
      }
      const targets = Array.isArray(d.targets) ? d.targets : [];
      this.utilityComboResult = {
        ok: true,
        count: d.target_count || targets.length,
        sample: targets.slice(0, 3),
        method: (d.gateway_response && d.gateway_response.method) || null,
        freeCount: d.free_model_count || 0,
      };
    } catch (e) {
      this.utilityComboResult = { ok: false, error: String(e) };
    } finally {
      this.utilityComboBusy = false;
    }
  },

  // ---- inline-dashboard computed getters (for the settings template) ----
  get dashModelCount() { return (this.dash && this.dash.provider_count) || 0; },
  get dashFreeCount()  { return (this.dash && this.dash.free_count) || 0; },
  get dashCheapCount() { return (this.dash && this.dash.cheap_count) || 0; },
  get dashKeyCount()   { return (this.dash && this.dash.key_count) || 0; },
  get dashSubCount()   { return (this.dash && this.dash.sub_count) || 0; },
  get dashLatency()    { return (this.dash && this.dash.latency_ms) || 0; },
  get dashBaseUrl()    { return (this.dash && this.dash.base_url) || ""; },
  get dashModels()     { return (this.dash && this.dash.models) || []; },
  get dashReachable()  { return !!(this.dash && this.dash.reachable) || this.installState === "ready"; },
  get pctFree()  { return this.dashModelCount ? (this.dashFreeCount  / this.dashModelCount) * 100 : 0; },
  get pctCheap() { return this.dashModelCount ? (this.dashCheapCount / this.dashModelCount) * 100 : 0; },
  get pctKey()   { return this.dashModelCount ? (this.dashKeyCount   / this.dashModelCount) * 100 : 0; },
  get pctSub()   { return this.dashModelCount ? (this.dashSubCount   / this.dashModelCount) * 100 : 0; },

  async test() {
    this.busy = true;
    this.testResult = null;
    try {
      const r = await fetch("/api/plugins/omniroute/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: "auto", prompt: "Reply with the single word: pong" }),
      });
      const data = await r.json();
      this.testResult = data;
      if (data.ok) {
        toastFrontendSuccess(`Test OK: "${data.reply}" (${data.latency_ms} ms)`, "OmniRoute");
      } else {
        toastFrontendError(`Test failed: ${data.error}`, "OmniRoute");
      }
    } catch (e) {
      toastFrontendError(String(e), "OmniRoute");
    } finally {
      this.busy = false;
    }
  },

  // ---- recover (one-click "Start OmniRoute" / "Recover") ----
  // Downloads the host-side installer as a .ps1 file so the user can
  // double-click it. Browsers can't execute host binaries directly, so
  // the save-then-run flow is the closest we can get to "one click"
  // without a native companion app. The installer is idempotent, so
  // re-running it is safe -- the same code path also handles the
  // "first install" case from the install wizard above.
  //
  // Why we schedule a 60 s delayed refresh: the install actually runs
  // on the host, not in the browser. The user has to confirm the UAC
  // prompt and let the script finish before the gateway comes back.
  // 60 s is long enough for "save file, double-click, UAC, script
  // runs, container starts, gateway probe completes" on a typical
  // Windows machine, short enough that the user doesn't have time to
  // forget why they clicked the button.
  async recoverGateway() {
    if (this.recovering) return;
    this.recovering = true;
    try {
      // Same URL the lazy-load in init() uses, per AGENTS.md
      // invariant #8: always /plugins/... (the built-in asset route),
      // never /usr/plugins/... (the user-plugin route -- the file
      // is there too, but only /plugins/... is guaranteed to serve
      // the file after a reinstall).
      const r = await fetch("/plugins/omniroute/webui/install-omniroute.ps1");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "install-omniroute.ps1";
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      // Give the browser a moment to start the download before we
      // remove the anchor and revoke the object URL.
      setTimeout(() => {
        try { document.body.removeChild(a); } catch (e) { /* DOM mutation never throws */ }
        URL.revokeObjectURL(url);
      }, 100);
      toastFrontendInfo(
        "Double-click the downloaded install-omniroute.ps1, confirm the UAC prompt, and wait ~30 s.",
        "OmniRoute"
      );
      // Schedule the auto re-detect. Clear any prior timer first
      // (the user might click the button twice in quick succession).
      if (this._recoverRefreshTimer) {
        clearTimeout(this._recoverRefreshTimer);
        this._recoverRefreshTimer = null;
      }
      this._recoverRefreshTimer = setTimeout(() => {
        this._recoverRefreshTimer = null;
        this.recovering = false;
        this.refresh();
      }, 60_000);
    } catch (e) {
      this.recovering = false;
      toastFrontendError("Could not download installer: " + e, "OmniRoute");
    }
  },

  // ---- remove gateway (one-click "Remove OmniRoute") ----
  // Mirror of recoverGateway() but downloads the uninstall script
  // (uninstall-omniroute.ps1) instead of the installer. The
  // gateway is independent infrastructure that may be in use by
  // other tools on the host (curl, scripts, other A0 plugins), so
  // the confirm() dialog is the safety belt. The user double-clicks
  // the downloaded .ps1; it stops + removes the container and
  // offers to remove the image.
  async uninstallGateway() {
    if (this.uninstalling) return;
    // Safety belt. Once the user confirms, the path is destructive
    // (host-side PowerShell that calls `docker rm`). The plugin
    // itself is unaffected: only the gateway is removed.
    const ok = window.confirm(
      "This will stop and remove the OmniRoute Docker container. " +
      "Other tools on this host using the gateway will lose access. " +
      "The Agent Zero plugin will stay installed but show \"Not installed\" " +
      "until you run the installer again. Continue?"
    );
    if (!ok) return;
    this.uninstalling = true;
    try {
      // AGENTS.md invariant #8: always /plugins/... (the built-in
      // asset route), never /usr/plugins/... (the user-plugin route
      // 404s the file after a reinstall).
      const r = await fetch("/plugins/omniroute/webui/uninstall-omniroute.ps1");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "uninstall-omniroute.ps1";
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      setTimeout(() => {
        try { document.body.removeChild(a); } catch (e) { /* DOM mutation never throws */ }
        URL.revokeObjectURL(url);
      }, 100);
      toastFrontendInfo(
        "Double-click uninstall-omniroute.ps1, confirm the UAC prompt, and wait ~10 s. The gateway will be removed.",
        "OmniRoute"
      );
      if (this._uninstallRefreshTimer) {
        clearTimeout(this._uninstallRefreshTimer);
        this._uninstallRefreshTimer = null;
      }
      this._uninstallRefreshTimer = setTimeout(() => {
        this._uninstallRefreshTimer = null;
        this.uninstalling = false;
        this.refresh();
      }, 60_000);
    } catch (e) {
      this.uninstalling = false;
      toastFrontendError("Could not download uninstaller: " + e, "OmniRoute");
    }
  },

  // ---- copy to clipboard ----
  async copyToClipboard(el, event) {
    const text = el?.querySelector("code")?.innerText || el?.innerText || "";
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        // Fallback for non-secure contexts (most localhost dev)
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      const btn = event?.currentTarget;
      if (btn) {
        const orig = btn.innerText;
        btn.innerText = "Copied!";
        btn.classList.add("copied");
        setTimeout(() => {
          btn.innerText = orig;
          btn.classList.remove("copied");
        }, 1500);
      }
      toastFrontendInfo("Copied to clipboard. Paste into a Windows PowerShell window.", "OmniRoute");
    } catch (e) {
      toastFrontendError("Copy failed: " + e, "OmniRoute");
    }
  },
});
