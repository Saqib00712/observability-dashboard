"""
Optional Phase 12 check (NOT part of pytest; needs `pip install playwright`
and an installed Chromium): loads the REAL dashboard in a real browser
against a seeded database, then verifies
  * no console errors / CSP violations on any tab,
  * numbers shown on screen equal the numbers the API returns,
  * a trace can be opened and its spans are listed.

    python scripts/seed_demo_data.py demo.db 200
    set SQLITE_PATH=demo.db & python -m uvicorn obsdash.main:app --port 8100
    python scripts/browser_check.py http://127.0.0.1:8100
"""

import json
import re
import sys
import urllib.request

from playwright.sync_api import sync_playwright

base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8100"
api = lambda p: json.load(urllib.request.urlopen(base + p))
problems, console = [], []

with sync_playwright() as pw:
    browser = pw.chromium.launch(executable_path=sys.argv[2] if len(sys.argv) > 2 else None)
    page = browser.new_page()
    page.on("console", lambda m: console.append((m.type, m.text)) if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: console.append(("pageerror", str(e))))
    page.goto(base + "/dashboard")
    page.wait_for_selector("#tab-overview *", timeout=10000)
    page.wait_for_timeout(1500)

    ov = api("/api/overview?window_minutes=60")
    page.select_option("#window", "60") if page.query_selector("#window option[value='60']") else None
    page.wait_for_timeout(1500)
    text = page.inner_text("#tab-overview")
    digits = lambda n: f"{n:,}"
    for label, val in (("requests", ov["requests"]["total"]), ("errors", ov["requests"]["errors"])):
        if digits(val) not in text and str(val) not in text:
            problems.append(f"overview does not show {label}={val}")
    print("overview shows total", ov["requests"]["total"], "->", "OK" if not problems else problems)

    for tab in ("ai", "traces", "errors", "alerts", "overview"):
        page.click(f'button[data-tab="{tab}"]')
        page.wait_for_timeout(1200)
        body = page.inner_text(f"#tab-{tab}")
        if len(body.strip()) < 20:
            problems.append(f"tab {tab} is empty")
        print(f"tab {tab:<8} rendered {len(body)} chars")

    ai = api("/api/overview?window_minutes=60")["ai"]
    page.click('button[data-tab="ai"]')
    page.wait_for_timeout(1200)
    ai_text = page.inner_text("#tab-ai")
    if ai["unpriced_calls"] and "unpriced" not in ai_text.lower() and "unknown" not in ai_text.lower():
        problems.append("unpriced calls exist but the AI tab does not say so")

    page.click('button[data-tab="traces"]')
    page.wait_for_timeout(1500)
    rows = page.query_selector_all("#tab-traces tr.click")
    print("trace rows found:", len(rows))
    if not rows:
        problems.append("no clickable trace rows")
    else:
        # pick a row with several spans so the tree has children
        target = next((r for r in rows if (r.inner_text().split("\t")[3:4] or ["0"])[0].strip().isdigit()
                       and int(r.inner_text().split("\t")[3].strip()) > 1), rows[0])
        target.scroll_into_view_if_needed()
        target.click()
        page.wait_for_selector("#tab-traces details", timeout=8000)
        page.wait_for_timeout(500)
        header = page.query_selector("#tab-traces h2:has-text('Trace ')")
        box = header.bounding_box() if header else None
        in_view = bool(box) and 0 <= box["y"] < page.viewport_size["height"]
        n_spans = len(page.query_selector_all("#tab-traces details"))
        print(f"opened trace: header in view={in_view}, span rows={n_spans}")
        if not in_view:
            problems.append("opened trace is not visible without scrolling")
        if n_spans < 2:
            problems.append("opened trace shows fewer than 2 spans")
        page.click("#tab-traces button:has-text('Close trace')")
        try:
            page.wait_for_selector("#tab-traces details", state="detached", timeout=8000)
        except Exception:  # noqa: BLE001
            problems.append("Close trace did not close the detail")
    browser.close()

bad = [c for c in console if "favicon" not in c[1]]
if bad:
    problems.append(f"console issues: {bad[:3]}")
print("console errors/warnings:", len(bad))
print("RESULT:", "PASS" if not problems else "FAIL " + "; ".join(problems))
sys.exit(1 if problems else 0)
