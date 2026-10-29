"""The app's page in Home Assistant (Settings → Apps → Tesla Bridge → Open Web UI).

Served only through the Supervisor's ingress proxy: the port is not mapped to
the host, and requests from anywhere but the proxy are refused. The page shows
what the app is doing and links back into Home Assistant, most importantly to
start the Tesla Bridge setup again if it was closed before it finished. The
app keeps the answers already given, so the setup fills them in.
"""

from __future__ import annotations

from collections.abc import Callable
import datetime as dt
from html import escape
from typing import Any

from aiohttp import web

from . import __version__
from .const import PUBLIC_KEY_PATH

# The Supervisor's ingress proxy; nothing else may use this page.
INGRESS_PROXY = "172.30.32.2"
# Paths in the Home Assistant frontend; the page runs in an iframe of it.
SETUP_URL = "/_my_redirect/config_flow_start?domain=tesla_bridge"
INTEGRATION_URL = "/config/integrations/integration/tesla_bridge"
REFRESH_SECONDS = 30

OK, WAIT, PROBLEM = "ok", "wait", "problem"
_ICONS = {OK: "✓", WAIT: "…", PROBLEM: "!"}


def _date(value: str | None) -> str:
    if not value:
        return ""
    try:
        return dt.datetime.fromisoformat(value).strftime("%Y-%m-%d")
    except ValueError:
        return value


def status_rows(status: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(state, title, detail) for each part of the app, in setup order."""
    rows: list[tuple[str, str, str]] = []
    domain = status.get("domain")
    configured = status.get("configured") or {}

    if domain:
        rows.append((OK, "Public name", domain))
    else:
        rows.append((WAIT, "Public name", "Not set yet. The setup asks for it."))

    duckdns = status.get("duckdns")
    if duckdns is None:
        if domain and not configured.get("duckdns_token"):
            rows.append((WAIT, "DuckDNS", "Not updated by this app (no token)."))
    elif duckdns.get("ok"):
        rows.append((OK, "DuckDNS", f"Points to {duckdns.get('ip')}"))
    elif duckdns.get("last_error"):
        rows.append((PROBLEM, "DuckDNS", str(duckdns["last_error"])))
    else:
        rows.append((WAIT, "DuckDNS", "Updating…"))

    key_server = status.get("key_server") or {}
    cert = key_server.get("certificate")
    if cert:
        rows.append(
            (
                OK,
                "Certificate",
                f"Valid until {_date(cert.get('not_after'))}, renewed automatically",
            )
        )
    elif key_server.get("last_error"):
        rows.append((PROBLEM, "Certificate", str(key_server["last_error"])))
    elif domain:
        rows.append((WAIT, "Certificate", "Requesting it from Let's Encrypt…"))

    public_key = key_server.get("public_key") or {}
    if public_key.get("served"):
        rows.append((OK, "Public key", f"Served at https://{domain}{PUBLIC_KEY_PATH}"))
    elif public_key.get("present"):
        rows.append((WAIT, "Public key", "Waiting for the certificate."))
    elif domain:
        rows.append(
            (WAIT, "Public key", "The setup sends it after you sign in to Tesla.")
        )

    telemetry = status.get("telemetry") or {}
    if telemetry.get("running"):
        rows.append((OK, "Streaming server", "Running"))
    elif telemetry.get("configured"):
        rows.append((PROBLEM, "Streaming server", "Not running; restarting it."))

    selftest = status.get("selftest") or {}
    if domain and not selftest.get("ran_at"):
        rows.append(
            (WAIT, "Router ports", "First checked about a minute after the app starts.")
        )
    elif selftest.get("ran_at"):
        failures = [
            str(check.get("detail"))
            for check in (
                selftest.get("key_loopback") or {},
                selftest.get("telemetry_loopback") or {},
            )
            if not check.get("ok")
        ]
        if not failures:
            rows.append(
                (
                    OK,
                    "Router ports",
                    f"443 and {status.get('telemetry_port')} answer on your "
                    "public address",
                )
            )
        else:
            # The check goes out to the public address and back in, which some
            # routers do not support even when the forwards work.
            rows.append(
                (
                    PROBLEM,
                    "Router ports",
                    "; ".join(failures) + ". Check the port forwards. Some routers "
                    "can't reach their own public address, so this can fail even "
                    "when the cars connect fine.",
                )
            )

    vehicles = (status.get("relay") or {}).get("vehicles") or {}
    cars = len(status.get("vins") or vehicles)
    if cars:
        streaming = sum(1 for v in vehicles.values() if v.get("connected"))
        today = sum(
            int((v.get("signals") or {}).get("count") or 0) for v in vehicles.values()
        )
        rows.append(
            (
                OK,
                "Cars",
                f"{cars} set up, {streaming} streaming now, "
                f"{today:,} values received today",
            )
        )
    return rows


def render_page(status: dict[str, Any]) -> str:
    connected = int((status.get("relay") or {}).get("subscribers") or 0) > 0
    if connected:
        intro = "Connected to the Tesla Bridge integration in Home Assistant."
        buttons = [("Open Tesla Bridge", INTEGRATION_URL, True)]
    else:
        intro = (
            "Not connected to the Tesla Bridge integration. If you closed the "
            "setup before it finished, continue it here: the answers you already "
            "gave are filled in. If Tesla Bridge is already set up, open it to "
            "see what's wrong."
        )
        buttons = [
            ("Continue setup", SETUP_URL, True),
            ("Open Tesla Bridge", INTEGRATION_URL, False),
        ]
    links = "".join(
        f'<a class="button{" primary" if primary else ""}" href="{escape(url)}" '
        f'target="_top">{escape(label)}</a>'
        for label, url, primary in buttons
    )
    items = "".join(
        f'<li class="{state}"><span class="icon">{_ICONS[state]}</span>'
        f"<div><strong>{escape(title)}</strong><br>{escape(detail)}</div></li>"
        for state, title, detail in status_rows(status)
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>Tesla Bridge</title>
<style>
:root {{ --bg: #fafafa; --card: #fff; --text: #212121; --muted: #616161;
  --line: #e0e0e0; --ok: #2e7d32; --wait: #8d6e00; --problem: #c62828;
  --accent: #03a9f4; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #111; --card: #1c1c1c; --text: #e1e1e1; --muted: #9e9e9e;
    --line: #2f2f2f; --ok: #81c784; --wait: #ffd54f; --problem: #ef9a9a; }}
}}
body {{ margin: 0; padding: 16px; background: var(--bg); color: var(--text);
  font: 15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
main {{ max-width: 640px; margin: 0 auto; }}
section {{ background: var(--card); border: 1px solid var(--line);
  border-radius: 12px; padding: 16px; margin-bottom: 16px; }}
h1 {{ font-size: 20px; margin: 0 0 4px; }}
p {{ margin: 0 0 12px; }}
.muted {{ color: var(--muted); font-size: 13px; }}
.buttons {{ display: flex; flex-wrap: wrap; gap: 8px; }}
.button {{ display: inline-block; padding: 8px 16px; border-radius: 8px;
  border: 1px solid var(--accent); color: var(--accent); text-decoration: none;
  font-weight: 500; }}
.button.primary {{ background: var(--accent); color: #fff; }}
ul {{ list-style: none; margin: 0; padding: 0; }}
li {{ display: flex; gap: 12px; padding: 10px 0; border-top: 1px solid var(--line);
  overflow-wrap: anywhere; }}
li:first-child {{ border-top: 0; }}
.icon {{ flex: none; width: 22px; height: 22px; border-radius: 50%;
  display: grid; place-items: center; font-size: 13px; font-weight: 700;
  color: var(--card); }}
.ok .icon {{ background: var(--ok); }}
.wait .icon {{ background: var(--wait); }}
.problem .icon {{ background: var(--problem); }}
</style>
</head>
<body>
<main>
<section>
<h1>Tesla Bridge</h1>
<p>{escape(intro)}</p>
<div class="buttons">{links}</div>
</section>
<section>
<ul>{items}</ul>
</section>
<p class="muted">App {escape(__version__)}. This page refreshes every
{REFRESH_SECONDS} seconds.</p>
</main>
</body>
</html>
"""


def create_webui_app(
    get_status: Callable[[], dict[str, Any]],
    allowed_remotes: frozenset[str] = frozenset({INGRESS_PROXY}),
) -> web.Application:
    @web.middleware
    async def ingress_only(request: web.Request, handler: Any) -> web.StreamResponse:
        if request.remote not in allowed_remotes:
            raise web.HTTPForbidden
        return await handler(request)

    async def index(request: web.Request) -> web.Response:
        return web.Response(
            text=render_page(get_status()),
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    app = web.Application(middlewares=[ingress_only])
    app.router.add_get("/", index)
    return app
