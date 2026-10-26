#!/usr/bin/env python3
"""
scrape_baseball.py
Scrapes Tommie Baseball hitting AND pitching stats from tommiesports.com
using Playwright (headless Chromium) since the page is JS-rendered via Sidearm Athletics.

Usage:
    python scraper/scrape_baseball.py
    python scraper/scrape_baseball.py --out docs/baseball_stats.json --year 2026
"""

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── CLI Args ──────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--out",  default="docs/baseball_stats.json")
parser.add_argument("--year", default="2026")
args = parser.parse_args()

STATS_URL = os.environ.get(
    "STATS_URL",
    f"https://tommiesports.com/sports/baseball/stats/{args.year}"
)
OUT_PATH = Path(args.out)
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────
def clean(text: str) -> str:
    return " ".join((text or "").split()).strip()


def parse_table(html: str, stat_type: str) -> list[dict]:
    """Parse a stats table from raw HTML into a list of row dicts."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if not table:
        print(f"  ⚠️  No <table> found in {stat_type} HTML")
        return []

    headers = [clean(th.get_text()) for th in table.select("thead th")]
    if not headers:
        headers = [clean(td.get_text()) for td in table.select("thead td")]

    rows = []
    for tr in table.select("tbody tr"):
        cells = [clean(td.get_text()) for td in tr.find_all(["td", "th"])]
        if not cells or all(c == "" for c in cells):
            continue
        # Pad or trim to match header length
        while len(cells) < len(headers):
            cells.append("")
        row = dict(zip(headers, cells))

        # Normalise the jersey number key
        number = (
            row.get("##") or row.get("#") or row.get("No") or
            row.get("No.") or row.get("NUM") or ""
        ).strip()
        row["number"] = number

        # Normalise player name key
        player = (
            row.get("Player") or row.get("Name") or row.get("PLAYER") or ""
        ).strip()
        row["player"] = player

        rows.append(row)

    print(f"  ✅  Parsed {len(rows)} {stat_type} rows ({len(headers)} columns)")
    return rows


def build_by_number(rows: list[dict]) -> dict:
    """Build a jersey-number-keyed lookup dict."""
    lookup = {}
    for r in rows:
        num = r.get("number", "").strip()
        if num:
            lookup[num] = r
    return lookup


# ── Main Scrape ───────────────────────────────────────────────────────────────
def scrape() -> dict:
    hitting_rows   = []
    pitching_rows  = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        # Block images/fonts/media to speed things up
        context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "font", "media")
            else route.continue_()
        )

        page = context.new_page()
        print(f"🌐  Loading {STATS_URL} ...")
        page.goto(STATS_URL, wait_until="networkidle", timeout=60_000)

        # ── HITTING ──────────────────────────────────────────────────────────
        print("🏏  Waiting for Hitting stats table...")
        try:
            page.wait_for_selector("table tbody tr", timeout=30_000)
        except PWTimeout:
            print("  ⚠️  Timed out waiting for hitting table")

        hitting_html = page.content()
        hitting_rows = parse_table(hitting_html, "hitting")

        # ── PITCHING — click the dropdown ────────────────────────────────────
        print("⚾  Switching to Pitching stats...")

        # Step 1: Open the stat-type dropdown (the trigger button/select)
        # The dropdown trigger is a button that contains the current selection text.
        # Try several selectors in order of specificity.
        dropdown_opened = False
        dropdown_selectors = [
            # Sidearm s-select trigger button
            "button.s-select__trigger",
            "[class*='s-select'] button",
            "[class*='s-select__trigger']",
            # Generic fallback — any button near a <select>
            "button[aria-haspopup='listbox']",
            "button[aria-expanded]",
        ]
        for sel in dropdown_selectors:
            try:
                trigger = page.locator(sel).first
                if trigger.is_visible(timeout=3_000):
                    trigger.click()
                    print(f"  ✅  Opened dropdown via: {sel}")
                    dropdown_opened = True
                    break
            except Exception:
                continue

        if not dropdown_opened:
            print("  ⚠️  Could not open dropdown — trying direct li click")
            # ── ADD THIS ──
            page.screenshot(path="debug_before_click.png")
            print("  📸  Screenshot saved → debug_before_click.png")

        # Step 2: Click the Pitching option by its exact element ID
        pitching_option_selectors = [
            "#v-5-0-0-2-1-1",                          # exact ID from the page
            "li[role='option']:has-text('Pitching')",   # role + text fallback
            "[role='option']:has-text('Pitching')",
            "li:has-text('Pitching')",
        ]
        pitching_clicked = False
        for sel in pitching_option_selectors:
            try:
                option = page.locator(sel).first
                option.wait_for(state="visible", timeout=8_000)
                option.click()
                print(f"  ✅  Clicked Pitching option via: {sel}")
                pitching_clicked = True
                break
            except Exception as e:
                print(f"  ↩️  Selector '{sel}' failed: {e}")
                continue

        if not pitching_clicked:
            print("  ❌  Could not click Pitching option — pitching stats will be empty")
            # ── ADD THIS ──
            page.screenshot(path="debug_dropdown.png")
            print("  📸  Screenshot saved → debug_dropdown.png")
        else:
            # Step 3: Wait for the table to re-render with pitching data
            try:
                # Wait for a column header that only appears in pitching tables
                page.wait_for_selector(
                    "table thead th:has-text('ERA'), "
                    "table thead th:has-text('IP'), "
                    "table thead th:has-text('W')",
                    timeout=15_000
                )
                print("  ✅  Pitching table detected")
            except PWTimeout:
                print("  ⚠️  Timed out waiting for pitching table re-render — scraping anyway")

            # Small buffer for any final JS paint
            page.wait_for_timeout(1_500)
            pitching_html = page.content()
            pitching_rows = parse_table(pitching_html, "pitching")

        browser.close()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
        "season": args.year,
        "source_url": STATS_URL,
        "hitting":            hitting_rows,
        "pitching":           pitching_rows,
        "hitting_by_number":  build_by_number(hitting_rows),
        "pitching_by_number": build_by_number(pitching_rows),
    }


# ── Entry Point ───────────────────────────────────────────────────────────────
def load_existing() -> dict:
    if OUT_PATH.exists():
        try:
            with open(OUT_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


if __name__ == "__main__":
    existing = load_existing()
    try:
        data = scrape()
        with open(OUT_PATH, "w") as f:
            json.dump(data, f, indent=2)
        print(f"\n✅  Saved → {OUT_PATH}")
        print(f"   Hitting rows:  {len(data['hitting'])}")
        print(f"   Pitching rows: {len(data['pitching'])}")
    except Exception:
        print("\n❌  Scrape failed — preserving last good data")
        traceback.print_exc()
        if existing:
            existing["status"] = "stale"
            existing["stale_since"] = datetime.now(timezone.utc).isoformat()
            with open(OUT_PATH, "w") as f:
                json.dump(existing, f, indent=2)
            print(f"   Stale data preserved at {OUT_PATH}")
        sys.exit(1)
