---
name: "omniroute-quickstart"
description: "Bring the OmniRoute gateway online and verify the A0 plugin can reach it, end-to-end. Use when the plugin's dashboard says offline, the user says 'install omniroute', 'omniroute not reachable', or 'test the gateway connection'."
author: "omniroute-plugin"
trigger_patterns:
  - "install omniroute"
  - "omniroute not reachable"
  - "omnirrite offline"
  - "omniroute dashboard offline"
  - "test omniroute connection"
  - "start omniroute gateway"
  - "omniroute quickstart"
metadata:
  source_plugin: "omniroute"
---

# OmniRoute Quickstart

## When to Use
Use this when the OmniRoute plugin is enabled but its dashboard shows
**Offline** (or shows "last seen" from hours ago), the user says
"install OmniRoute", "start the gateway", or asks you to verify the
plugin can reach its configured base URL. Also use after upgrading
the plugin or after moving the gateway to a different host/port.

## What the Plugin Already Did for You
The A0 `omniroute` plugin ships preconfigured. By the time you're
reading this skill:
- `conf/model_providers.yaml` registers the `omniroute/auto` model
  picker entry.
- `agents/omniroute/agent.yaml` is registered as a profile.
- `api/status.py` will return `reachable: true` once the gateway is up.
- The WebUI's plugin Settings page and dashboard page are already
  wired in `extensions/webui/`.

So you do NOT need to write config files, register agents, or open
ports. The only thing that's missing is the gateway process itself.

## The Process

### 1. Confirm the plugin thinks the gateway is offline
POST to the plugin's status endpoint and read the response:

```python
import requests
r = requests.post("http://localhost:50001/api/plugins/omniroute/status", json={})
d = r.json()
print("reachable:", d.get("reachable"), "error:", d.get("error"))
print("last_known:", d.get("last_known"))
```

If `reachable: true`, you're done — the gateway IS up. Skip to step 4
(verify the model picker).

If `last_known` is present, note its `age_seconds` and `latency_ms` —
that's what the user saw the last time it was working. Useful context
if the outage is recurring.

### 2. Run the bundled reachability probe
The plugin ships `scripts/check.py`. It uses the same stdlib HTTP
client the API handlers do, so its behavior is identical to what the
WebUI will see:

```bash
python /a0/usr/plugins/omniroute/skills/omniroute-quickstart/scripts/check.py
```

It exits 0 if the gateway is reachable, non-zero (with a reason) if
not. Use it to disambiguate: connection refused (gateway not started
yet) vs. timeout (gateway started but not listening on the right
port) vs. HTTP 4xx/5xx (gateway is up but auth/config wrong).

### 3. Start the gateway
The recommended way from inside the A0 container is to ask the user
to run the PowerShell installer surfaced in the WebUI:

1. Open the A0 WebUI → click the OmniRoute button in the chat-input bar
   (or visit `/usr/plugins/omniroute/webui/dashboard.html`).
2. Click the **Settings** button in the dashboard header.
3. The Settings page has an "Install OmniRoute" section that downloads
   `webui/install-omniroute.ps1` to the host. The user runs it in
   PowerShell.

If the user is on Linux/macOS or wants the manual path, the canonical
command is:
```bash
docker run -d --name omniroute -p 8080:20128 diegosouzapw/omniroute
```
(Note: the plugin defaults to host port 8080, not the upstream default
of 20128 — port 20128 is commonly blocked on Windows dev machines,
so the installer maps host 8080 -> container 20128.)

### 4. Verify the model picker
After the gateway is up, confirm the model picker is populated:

```python
r = requests.post("http://localhost:50001/api/plugins/omniroute/models", json={})
d = r.json()
print("count:", d.get("count"))
print("first 3:", [m["id"] for m in d.get("models", [])[:3]])
print("tiers:", d.get("tier_counts"))
```

`count` should be > 0 and `tiers` should show a non-zero `free` bucket
once the gateway has registered its free-tier providers.

If `count` is 0 but the probe in step 2 passed, the gateway is
running but hasn't loaded its provider list yet — wait 10 seconds and
retry, or check the gateway's own logs.

## Common Gotchas
- **Port 20128 vs. 8080**: the plugin's default is host 8080 (the
  PowerShell installer maps host 8080 -> container 20128, which is
  the upstream OmniRoute image's `ENV PORT`). If the user started
  OmniRoute on some other port, either change the plugin's
  `base_url` in Settings, or restart OmniRoute on the documented
  mapping.
- **Windows firewall**: the PowerShell installer adds a firewall
  rule. If the user started the container manually, port 8080 may
  still be blocked. The bundled installer handles this; the manual
  `docker run` does not.
- **A0 is in Docker, OmniRoute is on the host**: use
  `host.docker.internal:8080` (the plugin's default), NOT `localhost:8080`
  (that would be the container itself, not the host).
- **IPv6 / `::1`**: some setups map `localhost` to IPv6 first and the
  IPv4 connection fails. The plugin uses `host.docker.internal`
  which is IPv4-only — safer.

## Escalation
If after step 3 the gateway still isn't reachable:
- Have the user paste the full `omniroute-quickstart/scripts/check.py`
  output. The error message is specific enough to identify the
  failure mode (refused vs. timeout vs. 401 vs. 500).
- Do NOT loop retries. Two `check.py` calls, then surface the
  failure to the user with the specific error.
