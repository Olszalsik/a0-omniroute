# OmniRoute Plugin for Agent Zero

Connects Agent Zero to [**OmniRoute**](https://github.com/diegosouzapw/OmniRoute) —
a local AI gateway that exposes 230+ LLM providers through a single
OpenAI-compatible `/v1/*` endpoint, with 4-tier auto-fallback
(Subscription → API Key → Cheap → Free), token compression (RTK + Caveman,
15–95% savings), MCP/A2A support, and remote control tokens.

This plugin is the **thin client** that registers OmniRoute as a native
Agent Zero model provider. It is side-effect free: it does not install,
start, stop, or upgrade the OmniRoute Docker container. That separation
is deliberate (see [What this plugin is good for](#what-this-plugin-is-good-for)
and [Lifecycle](#lifecycle-install-disable-uninstall-remove-gateway)).

---

## Table of contents

1. [What is OmniRoute?](#what-is-omniroute)
2. [What this plugin is good for](#what-this-plugin-is-good-for)
3. [How to use](#how-to-use)
4. [Quickstart](#quickstart)
5. [Configuration](#configuration)
6. [Lifecycle: install, disable, uninstall, remove gateway](#lifecycle-install-disable-uninstall-remove-gateway)
7. [Troubleshooting](#troubleshooting)
8. [Architecture & contract](#architecture--contract)
9. [Running tests](#running-tests)
10. [License](#license)

---

## What is OmniRoute?

[OmniRoute](https://github.com/diegosouzapw/OmniRoute) is a self-hosted
gateway that sits between your applications and the rest of the LLM
ecosystem. Instead of integrating each provider (OpenAI, Anthropic,
Google, Groq, Mistral, OpenRouter, DeepSeek, Ollama, …) separately, you
point every client at one local URL and let OmniRoute handle the
fan-out.

The headline features:

- **230+ providers** behind a single OpenAI-compatible `/v1/*` endpoint.
- **4-tier auto-fallback.** When a request lands, OmniRoute tries
  Subscription first, then API Key, then Cheap, then Free — and only the
  Free tier is exposed without authentication, so unauthenticated
  installs are useful out of the box.
- **RTK + Caveman token compression.** Repeated boilerplate gets
  squeezed (15–95% fewer tokens on long agent traces). The plugin passes
  the response through unchanged — the savings show up in your agent's
  usage report.
- **MCP / A2A server access.** Tools and agent-to-agent servers that
  speak the Model Context Protocol are reachable through the gateway.
- **Remote control tokens** for the upstream OmniRoute admin UI.

See the upstream README for the full feature list, screenshots, and
configuration reference.

---

## What this plugin is good for

### When to install it

You want to **mix and match many LLM providers** without juggling API
keys in every Agent Zero subagent. The plugin registers OmniRoute as one
provider; OmniRoute does the fan-out. This is the use case the plugin
is designed for.

You want **one-click fallback when a quota runs out.** OmniRoute's
`auto/coding:free` and `auto/reasoning:free` aliases pick a free-tier
model automatically; the WebUI tier filter lets you flip between
"auto" and "auto/coding:free" with a single dropdown change when you
hit a paid-tier limit.

You want **lower token spend on long agent traces.** RTK + Caveman
compression runs on the gateway. The plugin does not parse or modify
the response — the savings show up in `api/usage.py`'s token counts.

You want **tool access (MCP / A2A) without per-agent configuration.**
The plugin speaks the standard OpenAI tool-call protocol; any agent
that already calls tools can reach MCP servers that OmniRoute exposes.

You want to **share a gateway with other tools on the same host.**
curl, scripts, VS Code extensions, and other A0 plugins can all hit
`http://localhost:8080/v1` independently. The container is just a local
HTTP server.

### When **not** to install it

You only ever need **one provider** (OpenAI, Anthropic, or local
Ollama). The plugin adds a network hop and a failure mode (the gateway
itself). Use the native provider config in `helpers/models.py` instead.

You cannot run Docker on the host (some locked-down enterprise Windows
machines, some HPC clusters). The plugin is happy to talk to OmniRoute
running any other way (npm, `cargo run`, source build, WSL2) — but the
recommended install path is the Docker container, and the WebUI's
"Download & run installer" button assumes Docker is available.

You need **strict per-call provider pinning** for compliance reasons
(every request must go to a specific vendor, no fallback allowed). The
plugin's `default_model` setting supports pinning a single model, but
if you need to enforce the pin at the framework level, register the
upstream provider directly so you control routing in code.

---

## How to use

Six entry points, in the order most users discover them:

### 1. The model picker

Open the chat UI and click the model dropdown. You will see an
**OmniRoute (auto)** provider. Pick it, then pick any `omniroute/*`
model. The full list is populated from `GET {base_url}/v1/models` and
refreshes whenever the gateway's catalog changes. Use the dropdown's
search box to filter (`claude`, `gpt`, `free`, …).

### 2. The OmniRoute Agent profile

In the model picker (or in *Settings → Agents*), pick the preconfigured
**OmniRoute Agent** profile. This is a `chat` subagent that already
uses the OmniRoute provider, has a `prompts/main.md` system prompt
that teaches it the tier-fallback workflow, and a matching
`skills/omniroute-quickstart/` skill it activates when you ask how to
bring the gateway online. If you are not sure where to start, use this
profile.

### 3. The status pill (top-right)

A small pill in the top-right corner of the WebUI shows the gateway
state. Green: online, with the model count. Red: offline. Yellow:
showing cached data because the live call failed. Click the pill to
open the dashboard.

### 4. The dashboard

Click the bottom-left **OmniRoute** chat-input button (or follow the
sidebar link) to open `webui/dashboard.html`. The dashboard shows:

- Tier breakdown bar (Free / Cheap / Key / Sub counts).
- A searchable, tier-filterable model list.
- Last-seen info if the live check failed.
- A "Refresh" button that re-probes the gateway.
- A "Start OmniRoute" button (when offline) that downloads
  `install-omniroute.ps1`.
- A "Remove gateway" button (when online) that downloads
  `uninstall-omniroute.ps1`.
- A "Settings" button that opens the plugin's config page.

### 5. The settings page

*Settings → External → OmniRoute* (or *Settings → Agent → OmniRoute*
on some builds) opens `webui/config.html`. From there you can:

- See the current install state and re-detect.
- Send a one-shot test completion to verify the round-trip.
- Load the full model list (with tier tags).
- Remove the gateway (see [Lifecycle](#lifecycle-install-disable-uninstall-remove-gateway)).
- Edit the advanced settings: base URL, API key, default model,
  timeout, preload-on-startup.

### 6. The status badge injector

The plugin ships an `extensions/webui/page-head/omniroute-status.html`
that injects the status pill into the top-right of every WebUI page.
It is purely additive — if the plugin is disabled or the gateway is
down, the pill degrades gracefully (red instead of throwing).

---

## Quickstart

Three steps to a working gateway. Total time: ~5 minutes plus the
~30 s the installer takes to bring the container up.

### 1. Install Docker Desktop

The recommended install path is the official Docker Desktop for
Windows. Get it from
[docker.com](https://www.docker.com/products/docker-desktop/). During
install, leave **Start Docker Desktop when you sign in** enabled
(the default) — see the [Keeping the gateway running](#keeping-the-gateway-running)
section for why this matters.

### 2. Bring the gateway online

From the Agent Zero WebUI:

1. Open *Settings → External → OmniRoute*.
2. Click **Download & run installer**. The browser downloads
   `install-omniroute.ps1`.
3. Double-click the downloaded file. Confirm the UAC prompt.
4. Wait ~30 seconds. The script pulls the `diegosouzapw/omniroute`
   image, starts the container with `--restart=unless-stopped`, and
   verifies the gateway is responding on `http://localhost:8080/v1`.

The status pill turns green within ~60 seconds (the WebUI polls on a
60 s delayed refresh after the installer download). If the pill is
still red, click **Re-detect** in the settings page or hit
[Troubleshooting](#troubleshooting).

You can also bring the container up by hand if you prefer:

```bash
docker run -d --name omniroute -p 8080:20128 --restart=unless-stopped diegosouzapw/omniroute
```

The plugin will detect it on next status check. (The WebUI installer
does this for you with extra verification — it is the recommended path.)

### 3. Pick a model and chat

In the chat UI model picker, choose **OmniRoute (auto)** as the
provider and any `omniroute/*` model. Or pick the **OmniRoute Agent**
profile for the preconfigured `auto` setup. Start chatting.

---

## Configuration

All settings are exposed in the WebUI's *Advanced settings* disclosure
on the settings page. They are also stored in
`usr/plugins/omniroute/config.json` and can be edited by hand.

| Setting | Default | Purpose |
|---|---|---|
| `base_url` | `http://localhost:8080/v1` | OmniRoute endpoint. Host port 8080 maps to the container's port 20128 (the upstream image's `ENV PORT`). We use 8080 on the host because 20128 is commonly blocked on Windows dev machines. |
| `api_key` | `""` | Bearer token. Only set if you enabled `OMNIROUTE_API_KEY` on the server. Empty by default = unauthenticated local mode. |
| `default_model` | `auto` | `auto` = let OmniRoute pick via tier-fallback. Or pin a specific model id (e.g. `openai/gpt-4o`, `anthropic/claude-3.5-sonnet`, `auto/coding:free`). |
| `timeout_seconds` | `60` | Per-request HTTP timeout. |
| `preload_models` | `true` | Cache the model list at agent startup so the model picker is instant. |
| `max_models_in_status` | `25` | Cap on the number of models shown in the status badge. |

The plugin also auto-detects the gateway on a small set of common
Docker-to-host addresses (`host.docker.internal`, `172.17.0.1`,
`gateway.docker.internal`) before falling back to `localhost`. You
should not have to override the URL unless your setup is unusual.

### Env-var overrides (advanced)

`OMNIROUTE_BASE_URL` and `OMNIROUTE_API_KEY` are read by the API
handlers and the live test suite. They are convenient for one-off
testing, but the canonical config lives in `config.json` and the
WebUI. Setting an env var and the WebUI to different values will give
you inconsistent behaviour — pick one source of truth.

---

## Lifecycle: install, disable, uninstall, remove gateway

The plugin's lifecycle and the gateway's lifecycle are **independent**.
The plugin folder can come and go without the container noticing. The
container can come and go without the plugin noticing. This table is
the whole story:

| Action | What happens to the container | What happens to the plugin |
|---|---|---|
| **Install plugin** (drop folder into `usr/plugins/`) | (none — container is set up separately) | Folder placed in `usr/plugins/omniroute/`, `hooks.install()` runs a non-blocking pre-flight probe. |
| **Toggle OFF plugin** (in *Settings → Plugins*) | Container keeps running | `.disabled` file in plugin folder. The plugin's code does not run; the A0 picker hides the provider. |
| **Toggle ON plugin** | Container keeps running | `.disabled` file removed. |
| **Uninstall plugin** (via *Settings → Plugins*) | Container keeps running | `hooks.uninstall()` logs (no side effects), folder deleted. |
| **Reinstall plugin** (drop folder back in) | Container keeps running | `hooks.install()` runs again. The gateway is immediately reachable if the container is still up. |
| **"Start OmniRoute"** (WebUI button, gateway offline) | Container is started (or created) | The WebUI button downloads `install-omniroute.ps1`; the user double-clicks it. |
| **"Remove OmniRoute gateway"** (WebUI button, gateway online) | Container is stopped, removed; user is prompted to also remove the image | Plugin stays installed. After the script runs, the status pill turns red and the settings page switches to the "Not installed" branch. |

The asymmetry is **intentional**. The plugin is a thin client. The
gateway is independent infrastructure that other tools (curl,
scripts, other A0 plugins) may also use. We provide both paths
(install/remove the container, install/uninstall the plugin) and do
not tie them together. This is documented as `AGENTS.md` invariant
#19.

### Keeping the gateway running

The PowerShell installer (`webui/install-omniroute.ps1`) sets up
auto-start with Docker's `--restart=unless-stopped` policy. The
container comes back automatically when the Docker daemon starts.
**The one caveat:** Docker Desktop must be set to start at logon for
this to work. The default is correct; if you have ever turned it off
(to shave a few seconds off logon time), the gateway will not come
back after a reboot and the WebUI's "Recover OmniRoute" button
becomes your morning ritual.

To verify the setting:
1. Open **Docker Desktop** from the Start menu.
2. Click the **gear icon** (top-right) → **General**.
3. Confirm **Start Docker Desktop when you sign in** is checked.

### Disabling the plugin

*Toggling the plugin off in Settings → Plugins* does **not** stop the
container. The toggle is a `.disabled`/`.enabled` file in the plugin
folder; A0's framework hides the plugin from the model picker and
the settings UI but does not run any Python code. The gateway keeps
serving other tools on the host.

To verify: `docker ps --filter name=omniroute` — the container is
still `Up`.

### Removing the gateway (without uninstalling the plugin)

Use the WebUI's **Remove OmniRoute gateway** button (settings page,
only shown when the gateway is online). The browser downloads
`uninstall-omniroute.ps1`; double-click it, confirm UAC, answer the
"also remove the image?" prompt (default: no — keep the image so
re-installs are instant), and wait ~10 seconds. The script does
`docker stop` + `docker rm` + an optional `docker rmi`. The plugin
stays installed but the status pill turns red.

The uninstall script is **idempotent**: running it when no container
exists prints *"OmniRoute container is not installed on this host.
Nothing to do."* and exits 0. Safe to run "just in case."

### Uninstalling the plugin

*Uninstalling the plugin via Settings → Plugins* does **not** stop
the container. The framework calls `hooks.uninstall()` (which logs
and returns) and then deletes the plugin folder. The gateway is
independent infrastructure and stays up. The framework log line is:

```
[omniroute] uninstall() called - no cleanup required
  The plugin folder will be removed by the framework. The OmniRoute
  Docker container is intentionally left running so other tools on
  this host (curl, scripts, other A0 plugins) keep working. To also
  remove the container, open Settings -> External -> OmniRoute and
  click 'Remove OmniRoute gateway' BEFORE uninstalling the plugin.
```

If you uninstalled the plugin and now want to clean up the container,
you have two options:

- **Reinstall the plugin** and use the WebUI button (the cleanest
  path).
- **Run docker directly** in PowerShell:
  `docker stop omniroute; docker rm omniroute` (and optionally
  `docker rmi diegosouzapw/omniroute` to free the ~500 MB image).

---

## Troubleshooting

### Status pill is red

The gateway is offline. Work the list:

1. Is the container running? `docker ps --filter name=omniroute`. If
   it is `Exited` or not present, the WebUI's "Start OmniRoute" /
   "Download & run installer" button is the recovery path. Double-click
   the downloaded `install-omniroute.ps1` and confirm UAC.
2. Is Docker Desktop running? Open it from the Start menu. If it is
   not running, the container cannot start. The installer waits up to
   60 s for the daemon to come up before it bails.
3. Is Docker Desktop set to start at logon? See [Keeping the
   gateway running](#keeping-the-gateway-running).
4. Is the host's Windows Firewall blocking port 8080? Open *Windows
   Security → Firewall & network protection → Advanced settings →
   Inbound Rules* and confirm port 8080 is allowed for private
   networks. Port 8080 is the default because the gateway's
   internal port 20128 is commonly blocked on Windows dev
   machines.

### "Gateway did not respond" during install

The installer's pre-flight probe (`GET {base_url}/v1/models`) timed
out after 45 s. The script's diagnostic dump shows:

- The last 50 lines of `docker logs omniroute --tail 50` — usually
  enough to spot a port conflict, a crashed upstream, or an OOM kill.
- The container state (`docker ps -a --filter name=omniroute`).

The most common cause: the host's Windows Firewall blocked the
container's published port. Test with `curl
http://localhost:8080/v1/models` from the same PowerShell window the
installer is running in. If curl returns a 200, the firewall is
allowing the loopback; if curl returns `Empty reply from server` or
`Connection refused`, the firewall is the issue.

### Model picker shows no models

`GET {base_url}/v1/models` returned an empty list. Either:

- The gateway is up but has no providers configured upstream. Open the
  upstream OmniRoute admin UI (the default URL is shown in the
  settings page) and add at least one provider.
- The cache is stale. The settings page's "Load model list" button
  bypasses the cache.
- The auto-detected base URL is wrong. Override the URL in
  *Settings → External → OmniRoute → Advanced settings* and click
  **Re-detect**.

### Tier filter dropdown is missing

The gateway is offline. The dropdown is populated from
`api/dashboard.py`, which falls back to a cached snapshot when the
live call fails. If the cache is empty (first run, gateway never up),
the dropdown is hidden. Bring the gateway online and reload.

### I uninstalled the plugin and the container is still running

That is by design. The plugin folder and the container are
independent. To clean up the container:

- Reinstall the plugin and click **Remove OmniRoute gateway** in the
  settings page, **or**
- Run `docker stop omniroute && docker rm omniroute` (and optionally
  `docker rmi diegosouzapw/omniroute`) in PowerShell.

### I see "missing API key" banners in the WebUI

The default install is unauthenticated. You should not see this
banner unless you set `OMNIROUTE_API_KEY` on the server. If you do,
enter the same value in *Settings → External → OmniRoute → Advanced
settings → API key*.

---

## Architecture & contract

The plugin is a thin client. It does not own the gateway; it talks to
one that you (or another tool) brought up independently.

- **Model provider registration.** `conf/model_providers.yaml` is
  merged into A0's provider list at startup by
  `helpers/providers.py:71-78`. The `api_base` field is a literal URL
  (no `{config.*}` placeholders are interpolated); the default is
  `http://host.docker.internal:8080/v1`. The agent profile
  `agents/omniroute/agent.yaml` + sibling `prompts/main.md` is picked
  up by `helpers/subagents.py:71-72`.
- **Reaching the gateway.** The plugin uses stdlib `urllib` only (no
  `requests`, no `aiohttp`). The HTTP client is
  `helpers/omniroute_client.py`; all five API handlers wrap sync
  `client.<method>()` calls in `asyncio.to_thread` via `*_async`
  wrappers, so A0's single asyncio event loop never blocks.
- **Side-effect free.** The plugin never installs pip packages,
  starts background services, registers scheduled tasks, or writes
  outside `usr/plugins/omniroute/`. It does **not** install,
  start, stop, or upgrade the OmniRoute Docker container (see
  `AGENTS.md` invariants #12 and #19).
- **`config.json` schema.** Lives at
  `usr/plugins/omniroute/config.json`. Contains `base_url`,
  `api_key`, `default_model`, `timeout_seconds`, `preload_models`,
  `max_models_in_status` (user-editable); `last_known` (plugin-local
  "last seen" snapshot, written by `helpers/last_known.py` via atomic
  tmp+rename); `models_cache` (dashboard accelerator, written by
  `helpers/cache.py` with the same atomic helper). `.disabled` is a
  sibling file, not a key.
- **Auto-detection.** The settings page probes
  `host.docker.internal:8080`, `172.17.0.1:8080`, and
  `gateway.docker.internal:8080` before falling back to
  `localhost:8080`. All four are tried in order; the first that
  responds wins.
- **Why the plugin does not install the gateway.** The plugin runs
  inside the A0 container. The gateway runs in a separate Docker
  container on the host. The plugin has no way to talk to the host's
  Docker daemon (and we do not want it to — that would be a
  privilege-escalation surface). Instead, the WebUI offers a
  PowerShell installer that the user double-clicks; the script runs
  on the host with the user's own credentials. Same model for
  removal.

For the full contract — every hard invariant, every cited A0 v2.5
mechanic, every "what this plugin does NOT do" — see `AGENTS.md`.

---

## Running tests

The plugin ships with two test suites.

### Smoke (CI, every push)

Runs in CI on every push and PR to `v2.5` that touches
`usr/plugins/omniroute/**`. Self-contained, no Docker, no gateway
required. The suite is the single source of truth for "is this
plugin healthy?" — every new feature ships with a test alongside
it.

```bash
python -m pytest usr/plugins/omniroute/tests/smoke.py -v
```

The CI workflow is `.github/workflows/omniroute-smoke.yml`. It
enforces the same check for every PR. Do not merge a red build.

### Live (by hand, before releases)

Runs against a real OmniRoute instance. Skipped by default (CI does
not collect it). Skipped at runtime if `OMNIROUTE_BASE_URL` is
unreachable (1.5 s TCP probe at module load).

```bash
OMNIROUTE_BASE_URL=http://localhost:8080/v1 \
  python -m pytest usr/plugins/omniroute/tests/live.py -v
```

Environment variables (all optional, all have safe defaults):

- `OMNIROUTE_BASE_URL` — gateway URL. Default
  `http://host.docker.internal:8080/v1`.
- `OMNIROUTE_API_KEY` — bearer token if your gateway requires one.
  Empty by default.
- `OMNIROUTE_LIVE_TEST_MODEL` — model used by the chat round-trip
  test. Default `openai/gpt-4o:free` (a known-free tier; override
  to validate a paid model).

If the gateway is down, every live test is skipped with a single
`SKIPPED` message. If the gateway is up but no upstream provider
is configured, `test_presets_chat_completions_each_endpoint`
exercises the three user-facing presets (`auto/best-free`,
`auto/coding:fast`, `auto/coding:free`) against
`/v1/chat/completions` and soft-fails on the documented
upstream-credential errors (401/403/404/408/503/504/timeout).
That test's purpose is to enumerate the failure modes so
maintainers can see at a glance whether the issue is
"gateway down", "gateway up but no upstream credentials", or
"actual regression in the helper" — the third case is the only
one that hard-fails.

---

## License

MIT — see `LICENSE`.
