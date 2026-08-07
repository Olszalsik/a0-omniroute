#!/usr/bin/env python3
"""
OmniRoute - gateway reachability probe.

Stdlib-only CLI the A0 agent (and humans) can run to confirm the
OmniRoute gateway is reachable from inside the A0 container. Uses
the SAME `OmniRouteClient.health()` the WebUI uses, so the result
is identical to what `/api/plugins/omniroute/status` will report.

Exit codes:
  0  gateway is reachable (HTTP 2xx, model list non-empty)
  1  gateway reachable but returned 0 models (probably still starting)
  2  gateway unreachable (connection refused, DNS, timeout)
  3  config file missing or unparseable
  4  unexpected error (printed to stderr)

Usage:
  python check.py                # use plugin's config.json base_url
  python check.py --url URL     # override the URL (testing a different gateway)
  python check.py --timeout 5   # shorter timeout
  python check.py --json        # emit a single-line JSON envelope (scripting)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# Add the plugin root AND its parent to sys.path so we can import the
# shared helper either as a namespace package (live A0 runtime, where
# /a0 is the script root) or as a direct module path (smoke tests).
PLUGIN_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
PLUGIN_PARENT = os.path.dirname(PLUGIN_ROOT)
for p in (PLUGIN_ROOT, PLUGIN_PARENT):
    if p and p not in sys.path:
        sys.path.insert(0, p)

try:
    from usr.plugins.omniroute.helpers.omniroute_client import (  # type: ignore
        OmniRouteClient,
        OmniRouteError,
    )
except ModuleNotFoundError:
    # Fall back to a direct import when the script is run outside the
    # live A0 runtime (no `usr.plugins` namespace on sys.path).
    from helpers.omniroute_client import (  # type: ignore  # noqa: F401
        OmniRouteClient,
        OmniRouteError,
    )

PLUGIN_NAME = "omniroute"


def _read_base_url_from_config() -> str:
    """Best-effort: pull base_url out of the plugin's config.json.

    Tolerates a missing or corrupt file; the default in
    `OmniRouteClient.__init__` is the same as the plugin's default
    (`http://host.docker.internal:8080/v1`), so we don't need to
    special-case "missing" beyond a debug log.
    """
    cfg_path = os.path.join(PLUGIN_ROOT, "config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        url = (cfg or {}).get("base_url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    except (OSError, json.JSONDecodeError):
        pass
    return ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Check whether the OmniRoute gateway is reachable.",
    )
    parser.add_argument(
        "--url", default=None,
        help="Override the base URL (default: plugin config.json -> autodetect).",
    )
    parser.add_argument(
        "--timeout", type=int, default=10,
        help="HTTP timeout in seconds (default: 10).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit a single-line JSON envelope instead of human-readable text.",
    )
    args = parser.parse_args(argv)

    if args.url:
        base_url = args.url.rstrip("/")
    else:
        base_url = _read_base_url_from_config()

    t0 = time.time()
    envelope = {
        "plugin": PLUGIN_NAME,
        "base_url": base_url,
        "ok": False,
        "provider_count": 0,
        "latency_ms": None,
        "error": None,
    }
    try:
        client = OmniRouteClient(base_url=base_url or None, timeout=args.timeout)
        envelope["base_url"] = client.base_url  # may differ if autodetect kicked in
        r = client.health()
        envelope["ok"] = bool(r.get("ok"))
        envelope["provider_count"] = int(r.get("provider_count") or 0)
        envelope["latency_ms"] = r.get("latency_ms")
        if not envelope["ok"]:
            envelope["error"] = r.get("error") or "gateway returned non-2xx"
    except OmniRouteError as e:
        envelope["error"] = str(e)
    except Exception as e:  # pragma: no cover
        envelope["error"] = f"unexpected: {e}"
    finally:
        envelope["elapsed_ms"] = int((time.time() - t0) * 1000)

    if args.json:
        print(json.dumps(envelope, sort_keys=True))
    else:
        if envelope["ok"]:
            print(
                f"OK  {envelope['base_url']}  "
                f"{envelope['provider_count']} models  "
                f"{envelope['latency_ms']}ms"
            )
        else:
            print(
                f"FAIL  {envelope['base_url']}  "
                f"({envelope['error']})"
            )

    # Exit code logic
    if envelope["ok"]:
        return 0 if envelope["provider_count"] > 0 else 1
    if envelope["error"]:
        return 2
    return 4


if __name__ == "__main__":
    sys.exit(main())
