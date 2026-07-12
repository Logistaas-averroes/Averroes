#!/usr/bin/env python3
"""Render the Search Terms + Patterns page at desktop/tablet/mobile widths (PR-ADS-144).

Serves ./static over a local HTTP server and intercepts /api/* with fixtures from
scripts/search_term_evidence_fixtures.json (generated from the REAL service with
mocked repositories, so shapes are exact). Screenshots:

  - Terms tab      @ 1440px
  - Patterns tab   @ 1440px
  - Term drawer    @ 1440px
  - Terms tab      @ 1024px (tablet)
  - Terms tab      @  390px (mobile)

Usage: python scripts/screenshot_search_terms.py [out_dir]
"""
from __future__ import annotations

import json
import os
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "scripts", "search_term_evidence_fixtures.json")


def _chrome_executable() -> str | None:
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
    def do_GET(self):  # noqa: N802
        if self.path == "/" or self.path.split("?", 1)[0] == "/":
            self.path = "/static/index.html"
        return super().do_GET()

    def log_message(self, *a):
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
        # Longest-key match so /patterns/detail wins over /patterns.
        for key in sorted(fixtures, key=len, reverse=True):
            if path == key or path.rstrip("/") == key:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(fixtures[key]))
                return
        route.fulfill(status=200, content_type="application/json", body="{}")

    written = []
    chrome = _chrome_executable()

    def open_page(ctx, hash_route):
        page = ctx.new_page()
        page.route("**/api/**", route_api)
        page.route("**/auth/**", route_api)
        page.goto(f"{base}/{hash_route}", wait_until="networkidle")
        page.wait_for_timeout(1200)
        page.evaluate(f"window.location.hash = '{hash_route}'")
        page.wait_for_timeout(1500)
        return page

    with sync_playwright() as pw:
        launch_kwargs = {"args": ["--no-sandbox"]}
        if chrome:
            launch_kwargs["executable_path"] = chrome
        browser = pw.chromium.launch(**launch_kwargs)

        def new_ctx(w, h):
            ctx = browser.new_context(viewport={"width": w, "height": h},
                                      device_scale_factor=2)
            ctx.add_cookies([{"name": "ads_session", "value": "screenshot",
                              "url": base}])
            return ctx

        def shot(page, name):
            out = os.path.join(out_dir, name)
            page.screenshot(path=out, full_page=True)
            written.append(out)

        # ── Desktop 1440: Terms, Term drawer, Patterns ──
        ctx = new_ctx(1440, 900)
        page = open_page(ctx, "#/search-terms")
        shot(page, "search-terms-terms-desktop-1440.png")
        # Whole row opens the evidence drawer — click the row matching the
        # drawer fixture so title and body show the same term.
        page.click('tr[data-st-term="freight%20forwarder%20jobs%20dubai"]')
        page.wait_for_timeout(1200)
        shot(page, "search-terms-term-drawer-desktop-1440.png")
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        page.click("#tab-btn-patterns")
        page.wait_for_timeout(1500)
        shot(page, "search-terms-patterns-desktop-1440.png")
        ctx.close()

        # ── Tablet 1024 ──
        ctx = new_ctx(1024, 800)
        page = open_page(ctx, "#/search-terms")
        shot(page, "search-terms-tablet-1024.png")
        ctx.close()

        # ── Mobile 390 ──
        ctx = new_ctx(390, 844)
        page = open_page(ctx, "#/search-terms")
        shot(page, "search-terms-mobile-390.png")
        ctx.close()

        browser.close()
    httpd.shutdown()
    for p in written:
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
