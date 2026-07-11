#!/usr/bin/env python3
"""Render the Campaign Evidence page at desktop/tablet/mobile widths (PR-ADS-143).

Serves ./static over a local HTTP server and intercepts /api/* with fixtures from
scripts/campaign_evidence_fixtures.json so the page renders with genuine
selected-window data (no live DB needed). Screenshots at 1440 / 1024 / 390 px.

Usage: python scripts/screenshot_campaign_evidence.py [out_dir]
"""
from __future__ import annotations

import json
import os
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static")
FIXTURES = os.path.join(ROOT, "scripts", "campaign_evidence_fixtures.json")

WIDTHS = [("desktop-1440", 1440, 900), ("tablet-1024", 1024, 800), ("mobile-390", 390, 844)]


def _chrome_executable() -> str | None:
    """Resolve a Chromium binary, preferring CHROME_BIN, then any pre-installed
    Playwright Chromium under PLAYWRIGHT_BROWSERS_PATH, else None (let Playwright
    use its own bundled browser). Never hardcodes a single version path."""
    import glob

    env = os.environ.get("CHROME_BIN")
    if env and os.path.exists(env):
        return env
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    for pat in ("chromium-*/chrome-linux/chrome",
                "chromium_headless_shell-*/chrome-linux/headless_shell"):
        matches = sorted(glob.glob(os.path.join(base, pat)))
        if matches:
            return matches[-1]
    return None


class _Handler(SimpleHTTPRequestHandler):
    """Serve the repo root so /static/* resolves; map / to static/index.html."""

    def do_GET(self):  # noqa: N802
        if self.path == "/" or self.path.split("?", 1)[0] == "/":
            self.path = "/static/index.html"
        return super().do_GET()

    def log_message(self, *a):  # silence request logging
        pass


def _serve() -> ThreadingHTTPServer:
    handler = partial(_Handler, directory=ROOT)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main() -> int:
    from playwright.sync_api import sync_playwright

    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "screenshots")
    os.makedirs(out_dir, exist_ok=True)
    with open(FIXTURES, encoding="utf-8") as fh:
        fixtures = json.load(fh)

    httpd = _serve()
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"

    def route_api(route):
        path = route.request.url.split(base, 1)[-1].split("?", 1)[0]
        for key, body in fixtures.items():
            if path == key or path.rstrip("/") == key:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(body))
                return
        # Unmatched /api → empty-but-valid JSON so the SPA boots cleanly.
        route.fulfill(status=200, content_type="application/json", body="{}")

    written = []
    chrome = _chrome_executable()
    with sync_playwright() as pw:
        launch_kwargs = {"args": ["--no-sandbox"]}
        if chrome:
            launch_kwargs["executable_path"] = chrome
        browser = pw.chromium.launch(**launch_kwargs)
        for name, w, h in WIDTHS:
            ctx = browser.new_context(viewport={"width": w, "height": h},
                                      device_scale_factor=2)
            # Seed a viewer session so the SPA does not gate on login.
            ctx.add_cookies([{"name": "ads_session", "value": "screenshot",
                              "url": base}])
            page = ctx.new_page()
            page.route("**/api/**", route_api)
            page.route("**/auth/**", route_api)
            page.goto(f"{base}/#/campaigns", wait_until="networkidle")
            page.wait_for_timeout(1200)
            # Ensure the campaigns route is active (SPA hash routing).
            page.evaluate("window.location.hash = '#/campaigns'")
            page.wait_for_timeout(1500)
            out = os.path.join(out_dir, f"campaign-evidence-{name}.png")
            page.screenshot(path=out, full_page=True)
            written.append(out)
            ctx.close()
        browser.close()
    httpd.shutdown()
    for p in written:
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
