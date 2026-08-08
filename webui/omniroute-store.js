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
// Returns ``null`` for a missing/unparseable base_url so callers can show
// a "not configured" toast instead of opening a broken tab.
//
// Duplicated (intentionally) in webui/dashboard.js — the dashboard uses its
// own Alpine scope and does not import the settings store, mirroring the
// existing recoverGateway()/uninstallGateway() duplication. Keep the two
// copies in sync.
export function gatewayWebUrl(base_url) {
  const raw = (base_url || "").trim();
  if (!raw) return null;
  try {
    const u = new URL(raw);
    let host = u.hostname;
    if (
      host === "host.docker.internal" ||
      host === "localhost" ||
      host === "127.0.0.1" ||
      host === "0.0.0.0"
    ) {
      host =
        (typeof window !== "undefined" &&
          window.location &&
          window.location.hostname) ||
        "localhost";
    }
    const port = u.port ? ":" + u.port : "";
    return `${u.protocol}//${host}${port}`;
  } catch (e) {
    return null;
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

  // ---- v2.6.5: gateway web-UI URL (browser-reachable) ----
  // Reads the configured base_url (or the last probed status.base_url) and
  // rewrites it to the gateway's own web UI root for the current browser
  // host. See the module-level gatewayWebUrl() helper for the transforms.
  get gatewayWebUrl() {
    const base =
      (this._config && this._config.base_url) ||
      (this.status && this.status.base_url) ||
      "";
    return gatewayWebUrl(base);
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
    this.refresh();
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
