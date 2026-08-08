// OmniRoute dashboard - Alpine component.
// Fetches aggregated status + models from /api/plugins/omniroute/dashboard,
// classifies each model into a tier (free/cheap/key/sub), and supports
// search + tier filter + free-only mode.
//
// Refresh model (Phase 4.1): two public methods, no auto-refresh yet.
//   - refresh()       : DEBOUNCED. Schedules a fetch 5s out, refreshing
//                       the pendingIn countdown so the button shows
//                       "Refresh in 3s…". Coalesces rapid calls (e.g.
//                       if a future auto-refresh hits refresh() every 5s
//                       the user also clicked, we only make one extra
//                       request, 5s after the most recent call).
//   - forceRefresh()  : IMMEDIATE. Cancels the pending debounce, runs
//                       the fetch now. Bound to the visible Refresh
//                       button so user clicks always feel responsive.
// Auto-refresh can be added later by calling refresh() in setInterval;
// the debounce keeps it from doubling up with manual clicks.

import { toastFrontendError, toastFrontendInfo } from "/components/notifications/notification-store.js";
// v2.6.1: import the omnirouteStore as the source of truth for the
// status pill. The config page and the chat-input topbar button both
// poll /api/plugins/omniroute/status via this store, so reading
// `store.status?.reachable` here keeps the dashboard's pill in sync
// with the rest of the UI. The dashboard endpoint (/api/plugins/
// omniroute/dashboard) is still used for the rich model data, but
// its `reachable` flag can lag behind the status endpoint (cache,
// cooldown, transient timeouts) and that's what caused the
// "dashboard says offline, config says online" discrepancy.
import { store as omnirouteStore } from "/plugins/omniroute/webui/omniroute-store.js";

const MODE_KEY = "omniroute.mode";
const DEBOUNCE_MS = 5000;

export function omnirouteDashboard() {
  return {
    busy: false,
    reachable: false,
    modelCount: 0,
    freeCount: 0,
    cheapCount: 0,
    keyCount: 0,
    subCount: 0,
    latency: 0,
    baseUrl: "",
    lastKnown: null,  // {ts, ts_iso, latency_ms, provider_count, base_url, reachable, age_seconds}
    models: [], // [{id, tier}]
    search: "",
    tierFilter: "",
    freeOnly: false,
    mode: "on",
    // Phase 5.1: cache state. `fromCache` is true iff the server
    // returned cached data (live call failed AND the cache's base_url
    // matched). `cacheAgeSeconds` comes from cached_snapshot.age_seconds.
    fromCache: false,
    cacheAgeSeconds: null,
    // Last error message from the dashboard API. Surfaced in the UI
    // so the user can tell "gateway offline" from "API error" from
    // "DNS resolution failed" (the previous version only showed
    // "Offline" with no diagnostic, leaving the user stuck).
    lastError: null,

    // Auto-recover state. Mirrors omnirouteStore.recoverGateway() in
    // webui/omniroute-store.js. The dashboard uses its own Alpine
    // scope (omnirouteDashboard) so it does not share the settings
    // page's store; we duplicate the small amount of state needed.
    recovering: false,
    _recoverRefreshTimer: null,
    // Auto-uninstall state. Mirrors omnirouteStore.uninstallGateway().
    // Same pattern: Blob download + 60 s refresh + confirm() guard.
    uninstalling: false,
    _uninstallRefreshTimer: null,

    // Debounce state. `pendingTimer` lives in the closure; the Alpine
    // component serializes by reference so we attach it as a non-reactive
    // hidden field (Alpine ignores keys it doesn't know about after init
    // IF you use $watch, but the safer pattern is to put it on `this` and
    // mark it ignored via x-data's `() => ({...})` factory — which we do).
    pendingTimer: null,
    pendingIn: 0,            // seconds until the next fetch, 0 = none pending
    pendingTicker: null,     // 1Hz ticker that decrements pendingIn for the button label
    lastFetchAt: 0,          // ms timestamp of the last completed fetch

    get modeLabel() {
      return { off: "Off", on: "All models", "free-only": "Free only" }[this.mode] || "On";
    },
    get pctFree()  { return this.modelCount ? (this.freeCount  / this.modelCount) * 100 : 0; },
    get pctCheap() { return this.modelCount ? (this.cheapCount / this.modelCount) * 100 : 0; },
    get pctKey()   { return this.modelCount ? (this.keyCount   / this.modelCount) * 100 : 0; },
    get pctSub()   { return this.modelCount ? (this.subCount   / this.modelCount) * 100 : 0; },

    get refreshLabel() {
      // The button label switches between three states:
      //   busy      -> "Refreshing..."
      //   pending   -> "Refresh in 3s…"
      //   idle      -> "Refresh"
      if (this.busy) return "Refreshing...";
      if (this.pendingIn > 0) return `Refresh in ${this.pendingIn}s…`;
      return "Refresh";
    },

    get lastKnownLabel() {
      // Only meaningful when live check failed AND we have a stored snapshot.
      if (!this.lastKnown || this.reachable) return null;
      const age = this.lastKnown.age_seconds;
      if (age == null) return null;
      let phrase;
      if (age < 60) phrase = "just now";
      else if (age < 3600) phrase = `${Math.floor(age / 60)} min ago`;
      else if (age < 86400) phrase = `${Math.floor(age / 3600)} h ago`;
      else phrase = `${Math.floor(age / 86400)} d ago`;
      const count = this.lastKnown.provider_count;
      const lat = this.lastKnown.latency_ms;
      return `Last seen ${phrase} (${count} models, ${lat}ms)`;
    },

    // Phase 5.1: pill text for the cache badge. Only shown when
    // fromCache is true. Mirrors the age bucketing in lastKnownLabel.
    get cachePill() {
      if (!this.fromCache) return null;
      const age = this.cacheAgeSeconds;
      if (age == null) return "cached";
      if (age < 60) return `cached ${age}s ago`;
      if (age < 3600) return `cached ${Math.floor(age / 60)} min ago`;
      if (age < 86400) return `cached ${Math.floor(age / 3600)} h ago`;
      return `cached ${Math.floor(age / 86400)} d ago`;
    },

    get filteredModels() {
      const q = (this.search || "").toLowerCase();
      return this.models.filter(m => {
        if (q && !m.id.toLowerCase().includes(q)) return false;
        if (this.tierFilter && m.tier !== this.tierFilter) return false;
        if (this.freeOnly && m.tier !== "free") return false;
        return true;
      });
    },

    init() {
      try {
        const saved = localStorage.getItem(MODE_KEY);
        if (saved) this.mode = saved;
      } catch (e) {}
      // Fetch immediately on init so the user sees fresh status
      // within ~1s instead of waiting 5s for the debounce. The
      // debounce is for subsequent calls (auto-refresh, manual
      // double-clicks) — the first call should be eager.
      this.forceRefresh();
      // Also sync the status pill with the omnirouteStore, which is the
      // source of truth used by the config page and the topbar OmniRoute
      // button. Without this, the dashboard reads /api/plugins/omniroute/
      // dashboard and the config page reads /api/plugins/omniroute/status;
      // the two endpoints can disagree (cache, cooldown, transient timeouts)
      // and the user sees "dashboard offline, config online". Pulling the
      // store on init makes the dashboard pill consistent with the rest
      // of the UI on first paint.
      try {
        if (omnirouteStore && typeof omnirouteStore.refresh === "function") {
          omnirouteStore.refresh().then(() => {
            if (omnirouteStore.status && typeof omnirouteStore.status.reachable === "boolean") {
              this.reachable = !!omnirouteStore.status.reachable;
            }
          }).catch(() => { /* standalone page: store refresh failed, keep dashboard's own result */ });
        }
      } catch (e) { /* sync best-effort */ }
    },

    // ---------------------------------------------------------- refresh
    //
    // DEBOUNCED public entry. If a fetch is in flight, we wait for it to
    // finish. Otherwise we schedule a fetch DEBOUNCE_MS from now and
    // start a 1Hz countdown. Repeated calls within the window just
    // reset the timer (latest-call-wins), which is the desired behavior:
    // a future auto-refresh + a user click shouldn't fire two requests.
    refresh() {
      this._clearPendingTicker();
      this._clearPendingTimer();
      this.pendingIn = Math.ceil(DEBOUNCE_MS / 1000);
      this.pendingTimer = setTimeout(() => {
        this.pendingTimer = null;
        this._clearPendingTicker();
        this._doFetch();
      }, DEBOUNCE_MS);
      this._startPendingTicker();
    },

    // IMMEDIATE public entry. Cancels the debounce and fires the fetch
    // right now. Bound to the Refresh button so user clicks always feel
    // responsive. If a fetch is already in flight, do nothing (the user
    // gets the result in a moment).
    forceRefresh() {
      if (this.busy) return;
      this._clearPendingTimer();
      this._clearPendingTicker();
      this._doFetch();
    },

    // The actual fetch — private. Splits the side effects from the
    // scheduling so tests can stub _doFetch() if they ever need to.
    async _doFetch() {
      this.busy = true;
      try {
        const r = await fetch("/api/plugins/omniroute/dashboard", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const d = await r.json();
        this.modelCount = d.provider_count || 0;
        this.freeCount = d.free_count || 0;
        this.cheapCount = d.cheap_count || 0;
        this.keyCount = d.key_count || 0;
        this.subCount = d.sub_count || 0;
        this.latency = d.latency_ms || 0;
        this.baseUrl = d.base_url || "";
        this.models = d.models || [];
        this.fromCache = !!d.from_cache;
        // cacheAgeSeconds: pulled from cached_snapshot.age_seconds when
        // present. Stays null when no cache snapshot was returned.
        this.cacheAgeSeconds = (d.cached_snapshot && d.cached_snapshot.age_seconds != null)
          ? d.cached_snapshot.age_seconds
          : null;
        this.lastKnown = d.last_known || null;
        this.lastError = d.error || null;
        this.lastFetchAt = Date.now();
        // ---- reconcile reachable flag across endpoints ----
        // The config page and the topbar button both poll
        // /api/plugins/omniroute/status (via omnirouteStore). The
        // dashboard endpoint (/api/plugins/omniroute/dashboard) can
        // disagree because of the cache layer in backend/api/dashboard.py:
        // when the live call fails but a same-base_url cache exists,
        // it returns reachable=False even though the gateway itself
        // is responding. To keep the dashboard pill in sync with the
        // rest of the UI:
        // 1. Prefer the omnirouteStore status if it has been populated.
        // 2. Otherwise, accept the dashboard endpoint's verdict.
        if (omnirouteStore && omnirouteStore.status && typeof omnirouteStore.status.reachable === "boolean") {
          this.reachable = !!omnirouteStore.status.reachable;
        } else {
          this.reachable = !!d.reachable;
        }
      } catch (e) {
        this.reachable = false;
        this.lastError = String(e);
        toastFrontendError("Failed to fetch dashboard: " + e, "OmniRoute");
      } finally {
        this.busy = false;
        // If another refresh() call landed while we were fetching, the
        // pendingTimer is still set and the next fire is already queued.
      }
    },

    // 1Hz ticker that decrements pendingIn for the button label.
    _startPendingTicker() {
      if (this.pendingTicker) return;
      this.pendingTicker = setInterval(() => {
        if (this.pendingIn > 0) this.pendingIn -= 1;
        if (this.pendingIn <= 0) this._clearPendingTicker();
      }, 1000);
    },
    _clearPendingTicker() {
      if (this.pendingTicker) {
        clearInterval(this.pendingTicker);
        this.pendingTicker = null;
      }
    },
    _clearPendingTimer() {
      if (this.pendingTimer) {
        clearTimeout(this.pendingTimer);
        this.pendingTimer = null;
      }
      this.pendingIn = 0;
    },

    // ----------------------------------------------------------------------
    openSettings() {
      // v2.6.1: ALWAYS navigate directly to the config page. The previous
      // version (v2.5) tried pluginSettingsPrototype.openConfig() first,
      // which is the modal-based navigation used in the agent-profile
      // chrome. When the dashboard is loaded as a standalone page (the
      // common case — user opens the dashboard via the topbar
      // "OmniRoute" button or via /usr/plugins/omniroute/webui/dashboard.html),
      // the prototype EITHER is not registered OR is registered but
      // openConfig silently does not navigate, so the function returned
      // without ever hitting the window.location fallback. Result: all
      // three Settings buttons (header, Mode card, offline empty state)
      // did nothing on click. Removing the prototype attempt:
      // - always lands the user on the plugin settings page;
      // - keeps the same destination URL the config page itself expects;
      // - removes the dead-code path that caused the user-reported bug.
      try {
        // AGENTS.md invariant #8: built-in asset route /plugins/...
        // (always serves, survives reinstalls). NOT /usr/plugins/...
        // (404 after reinstall). Same URL recoverGateway() uses below.
        window.location.href = "/plugins/omniroute/webui/config.html";
      } catch (e) {
        toastFrontendError("Cannot open settings: " + e, "OmniRoute");
      }
    },

    async copyModel(id) {
      try {
        await navigator.clipboard.writeText(id);
        toastFrontendInfo(`Copied "${id}" to clipboard`, "OmniRoute");
      } catch (e) {
        toastFrontendError("Copy failed: " + e, "OmniRoute");
      }
    },

    // ----------------------------------------------------------------------
    // Download the host-side installer and trigger a refresh after 60 s.
    // Same UX as omnirouteStore.recoverGateway() in webui/omniroute-store.js
    // — duplicated here because the dashboard uses its own Alpine scope
    // and does not import the settings store. Browsers cannot execute
    // host binaries directly, so the user double-clicks the downloaded
    // .ps1 to actually run it. The installer is idempotent, so re-runs
    // are safe (the WebUI's "Recover" path is the same code as the
    // "first install" path).
    async recoverGateway() {
      if (this.recovering) return;
      this.recovering = true;
      try {
        // AGENTS.md invariant #8: always /plugins/... (the built-in
        // asset route), never /usr/plugins/... (the user-plugin route
        // 404s the file after a reinstall).
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
        setTimeout(() => {
          try { document.body.removeChild(a); } catch (e) { /* DOM mutation never throws */ }
          URL.revokeObjectURL(url);
        }, 100);
        toastFrontendInfo(
          "Double-click the downloaded install-omniroute.ps1, confirm the UAC prompt, and wait ~30 s.",
          "OmniRoute"
        );
        if (this._recoverRefreshTimer) {
          clearTimeout(this._recoverRefreshTimer);
          this._recoverRefreshTimer = null;
        }
        this._recoverRefreshTimer = setTimeout(() => {
          this._recoverRefreshTimer = null;
          this.recovering = false;
          this.forceRefresh();
        }, 60_000);
      } catch (e) {
        this.recovering = false;
        toastFrontendError("Could not download installer: " + e, "OmniRoute");
      }
    },

    // ----------------------------------------------------------------------
    // Download the host-side uninstaller and trigger a refresh after 60 s.
    // Mirror of recoverGateway() above, but downloads
    // uninstall-omniroute.ps1 (which does `docker stop` + `docker rm`
    // and prompts to remove the image). The confirm() guard is the
    // safety belt: once the user OKs, the host-side PowerShell is
    // destructive and the gateway will be gone.
    async uninstallGateway() {
      if (this.uninstalling) return;
      const ok = window.confirm(
        "This will stop and remove the OmniRoute Docker container. " +
        "Other tools on this host using the gateway will lose access. " +
        "The Agent Zero plugin will stay installed but show \"Not installed\" " +
        "until you run the installer again. Continue?"
      );
      if (!ok) return;
      this.uninstalling = true;
      try {
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
          this.forceRefresh();
        }, 60_000);
      } catch (e) {
        this.uninstalling = false;
        toastFrontendError("Could not download uninstaller: " + e, "OmniRoute");
      }
    },
  };
}

if (typeof window !== "undefined") {
  window.omnirouteDashboard = omnirouteDashboard;
}
