#!/usr/bin/env python3
"""Verify Cluster Ingest dashboard controls in a real browser (Playwright/Chromium)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://10.44.40.41:5601"
ROOT = Path(__file__).parent
elastic_pw = (ROOT / "secrets" / "elastic-password").read_text().strip()

DASHBOARDS = [
    (
        "elasticsearch-b1399af0-628c-11ee-9c63-732d7f759a7a",
        "cluster-node",
        "Cluster & Node View",
    ),
    (
        "elasticsearch-ea888f80-61e4-11ee-b5a1-0d1803efe5cf",
        "index-shard",
        "Index & Shard View",
    ),
]


def main() -> int:
    ok = True
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        failed_requests: list[str] = []

        def on_response(resp):
            url = resp.url
            if "/internal/controls/optionsList/" in url:
                if resp.status >= 400:
                    failed_requests.append(f"{resp.status} {url[:120]}")
                else:
                    try:
                        data = resp.json()
                        if not (data.get("suggestions") or []):
                            failed_requests.append(f"empty {url[:120]}")
                    except Exception:
                        pass

        page.on("response", on_response)

        page.goto(f"{BASE}/login", wait_until="networkidle", timeout=120000)
        page.fill('input[data-test-subj="loginUsername"]', "elastic")
        page.fill('input[data-test-subj="loginPassword"]', elastic_pw)
        page.click('button[data-test-subj="loginSubmit"]')
        page.wait_for_url(re.compile(r".*/app/.*"), timeout=120000)

        for dash_id, slug, label in DASHBOARDS:
            print(f"\n=== Browser check: {label} ===", flush=True)
            failed_requests.clear()

            page.goto(
                f"{BASE}/app/dashboards#/view/{dash_id}?_g=(time:(from:now-7d,to:now))",
                wait_until="networkidle",
                timeout=120000,
            )
            page.wait_for_timeout(10000)

            # Dashboard controls use euiComboBox in Kibana 8.x
            combo_boxes = page.locator('[data-test-subj="comboBoxInput"]').all()
            print(f"  combo boxes: {len(combo_boxes)}", flush=True)

            control_ok = True
            for i, combo in enumerate(combo_boxes[:2]):
                combo.click(timeout=5000)
                page.wait_for_timeout(2000)
                error_el = page.locator('text="An error occurred"')
                if error_el.count() > 0:
                    print(f"  FAIL control {i}: shows 'An error occurred'", flush=True)
                    control_ok = False
                    ok = False
                else:
                    options = page.locator('[role="option"]').all()
                    print(f"  control {i}: options={len(options)}", flush=True)
                    if not options:
                        print(f"  WARN control {i}: no options visible", flush=True)
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)

            if failed_requests:
                print("  FAILED optionsList requests:", flush=True)
                for req in failed_requests:
                    print(f"    {req}", flush=True)
                ok = False
            else:
                print("  OK: optionsList API requests succeeded", flush=True)

            if control_ok:
                print("  OK: controls do not show error state", flush=True)

            screenshot = ROOT / f"cluster_controls_{slug}.png"
            page.screenshot(path=str(screenshot), full_page=False)
            print(f"  screenshot: {screenshot}", flush=True)

        browser.close()

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())