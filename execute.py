"""
OmniRoute plugin - user-triggered maintenance script.

Run from the Plugins UI (or directly: `python /a0/usr/plugins/omniroute/execute.py`).

Verifies:
  1. All required plugin files are in place.
  2. plugin.yaml parses and reports the expected version.
  3. OmniRoute is reachable at the configured base_url (if reachable,
     prints provider count + sample model IDs).

Returns 0 on success, non-zero on failure. Safe to re-run.
"""

import json
import os
import sys

PLUGIN_NAME = "omniroute"
EXPECTED_VERSION = "2.6.9"


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    print(f"[{PLUGIN_NAME}] Plugin dir: {here}")

    # --- 1. Required files ---
    required = [
        "plugin.yaml",
        "default_config.yaml",
        "hooks.py",
        "conf/model_providers.yaml",
        "agents/omniroute/agent.yaml",
        "api/status.py",
        "api/models.py",
        "api/test.py",
        "helpers/omniroute_client.py",
        "webui/config.html",
        "webui/omniroute-store.js",
        "webui/install-omniroute.ps1",
        "extensions/webui/page-head/omniroute-status.html",
        "extensions/webui/chat-input-bottom-actions-end/omniroute-button.html",
    ]
    missing = [p for p in required if not os.path.isfile(os.path.join(here, p))]
    if missing:
        print(f"[{PLUGIN_NAME}] ERROR: missing files:")
        for m in missing:
            print(f"  - {m}")
        return 1
    print(f"[{PLUGIN_NAME}] OK: all {len(required)} required files present")

    # --- 2. Manifest sanity ---
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None

    with open(os.path.join(here, "plugin.yaml"), "r", encoding="utf-8") as f:
        manifest = f.read()
    if yaml is not None:
        try:
            data = yaml.safe_load(manifest)
        except Exception as e:
            print(f"[{PLUGIN_NAME}] ERROR: plugin.yaml is not valid YAML: {e}")
            return 2
    else:
        data = {}
        for line in manifest.splitlines():
            if ":" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition(":")
                data[k.strip()] = v.strip()

    name = (data.get("name") or "").strip()
    version = (data.get("version") or "").strip()
    if name != PLUGIN_NAME:
        print(f"[{PLUGIN_NAME}] ERROR: plugin name is {name!r}, expected {PLUGIN_NAME!r}")
        return 3
    if version != EXPECTED_VERSION:
        print(f"[{PLUGIN_NAME}] WARN: version is {version!r}, expected {EXPECTED_VERSION!r}")
    else:
        print(f"[{PLUGIN_NAME}] OK: manifest version {version}")

    # --- 3. OmniRoute reachability ---
    try:
        # Late import so the file-presence checks above still pass even if
        # the helper can't be imported in the CLI environment.
        from usr.plugins.omniroute.helpers.omniroute_client import OmniRouteClient  # type: ignore

        # Best-effort: prefer the active config if available, else default.
        base_url = "http://host.docker.internal:8080/v1"
        try:
            import yaml as _y  # type: ignore
            with open(os.path.join(here, "default_config.yaml"), "r", encoding="utf-8") as f:
                cfg = _y.safe_load(f) or {}
            base_url = cfg.get("base_url") or base_url
        except Exception:
            pass

        client = OmniRouteClient(base_url=base_url, timeout=10)
        health = client.health()
        if health.get("ok"):
            print(f"[{PLUGIN_NAME}] OK: OmniRoute reachable at {base_url} ({health.get('latency_ms')} ms, {health.get('provider_count')} models)")
            print(f"[{PLUGIN_NAME}] sample models: {health.get('sample_models')}")
        else:
            print(f"[{PLUGIN_NAME}] WARN: OmniRoute not reachable at {base_url}")
            print(f"[{PLUGIN_NAME}]   {health.get('error')}")
            print(f"[{PLUGIN_NAME}]   Start it with: docker run -d -p 8080:8080 diegosouzapw/omniroute")
            print(f"[{PLUGIN_NAME}]   (or run the PowerShell installer from the WebUI dashboard)")
    except Exception as e:
        print(f"[{PLUGIN_NAME}] WARN: could not import OmniRoute client: {e}")

    # --- 4. Toggle state ---
    toggle_on = os.path.isfile(os.path.join(here, ".toggle-1"))
    toggle_off = os.path.isfile(os.path.join(here, ".toggle-0"))
    if toggle_on:
        state = "ON"
    elif toggle_off:
        state = "OFF"
    else:
        state = "DEFAULT (enabled)"
    print(f"[{PLUGIN_NAME}] Toggle state: {state}")

    # --- 5. Summary ---
    print()
    print(json.dumps({
        "plugin": PLUGIN_NAME,
        "version": version,
        "manifest_version_expected": EXPECTED_VERSION,
        "toggle_state": state,
        "all_files_present": True,
    }, indent=2))
    print()
    print(f"[{PLUGIN_NAME}] Health check PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
