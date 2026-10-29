#!/usr/bin/env python3
"""
scrape_baseball.py
──────────────────
Fetch St. Thomas Tommies Baseball / Softball cumulative player stats from:
  https://tommiesports.com/sports/{sport}/stats/{year}

Extracts Batters (Hitting) and Pitchers (Pitching) correctly using Playwright tab
navigation and header-based table validation.

Usage:
    pip install playwright beautifulsoup4
    playwright install chromium

    python scrape_baseball.py --out docs/baseball_stats.json --year 2026
"""

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

# ── Constants ─────────────────────────────────────────────────────────────────

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

BASE_URL = "https://tommiesports.com/sports/{sport}/stats/{year}"

# Exact requested Batting metrics (and their column header aliases)
HITTING_COLS = [
    ("AVG",  ["AVG", "BA", "BATTINGAVG"]),
    ("R",    ["R", "Runs", "RUN"]),
    ("H",    ["H", "Hits", "HIT"]),
    ("2B",   ["2B", "DOUBLES"]),
    ("3B",   ["3B", "TRIPLES"]),
    ("HR",   ["HR", "HOMERUNS"]),
    ("RBI",  ["RBI", "RBIS"]),
    ("OPS",  ["OPS"]),
    ("SLG%", ["SLG%", "SLG", "SLG_PCT", "SLUGGING%"]),
]

# Exact requested Pitching metrics (and their column header aliases)
PITCHING_COLS = [
    ("ERA",  ["ERA", "EARNEDRUNAVG"]),
    ("WHIP", ["WHIP"]),
    ("W-L",  ["W-L", "WL", "W_L", "REC", "RECORD"]),
    ("IP",   ["IP", "Innings", "INNINGSPITCHED"]),
    ("SO",   ["SO", "K", "SO/K", "STRIKEOUTS"]),
]


# ── String & Table Helpers ────────────────────────────────────────────────────

def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def norm_header(h: str) -> str:
    """Normalise a header string for case/punctuation-insensitive matching."""
    return re.sub(r"[^a-z0-9]", "", h.lower())


def header_cells(table) -> list[str]:
    row = table.select_one("thead tr") or table.find("tr")
    return [clean(c.get_text()) for c in row.find_all(["th", "td"])] if row else []


def body_rows(table) -> list:
    rows = table.select("tbody tr") or table.find_all("tr")[1:]
    return [r for r in rows if r.find_all(["td", "th"])]


def cell_text(cells: list, idx: int | None) -> str:
    if idx is None or idx >= len(cells):
        return ""
    return clean(cells[idx].get_text())


def find_col(hdr_map: dict, *aliases) -> int | None:
    """Return column index for the first matching alias, or None."""
    for alias in aliases:
        idx = hdr_map.get(norm_header(alias))
        if idx is not None:
            return idx
    return None


def jersey_clean(raw: str) -> str:
    """Strip non-numeric characters; return empty string if none present."""
    return re.sub(r"[^0-9]", "", raw)


def has_player_col(headers: list[str]) -> bool:
    normed = [norm_header(h) for h in headers]
    return any("player" in h or "name" in h for h in normed)


def is_hitting_table(headers: list[str]) -> bool:
    normed = [norm_header(h) for h in headers]
    has_hit = any(h in normed for h in ["avg", "ab", "slg", "slgpct", "rbi", "ops"])
    has_pitch = any(h in normed for h in ["era", "ip", "whip", "sv", "bavg"])
    return has_hit and not has_pitch


def is_pitching_table(headers: list[str]) -> bool:
    normed = [norm_header(h) for h in headers]
    return any(h in normed for h in ["era", "ip", "whip", "so", "app"])


# ── Parsers ───────────────────────────────────────────────────────────────────

def parse_hitting_table(table) -> list[dict]:
    headers = header_cells(table)
    hdr_map = {norm_header(h): i for i, h in enumerate(headers)}

    num_idx = find_col(hdr_map, "##", "#", "No", "Num")
    player_idx = find_col(hdr_map, "Player", "Name")

    if player_idx is None:
        return []

    # Map target metrics
    col_indices = {
        out_key: find_col(hdr_map, *aliases)
        for (out_key, aliases) in HITTING_COLS
    }

    # Backup columns to calculate OPS if Sidearm doesn't provide it directly
    obp_idx = find_col(hdr_map, "OB%", "OBP", "ONBASE%")
    slg_idx = find_col(hdr_map, "SLG%", "SLG", "SLUGGING%")

    rows = []
    for tr in body_rows(table):
        cells = tr.find_all(["td", "th"])
        raw_player = cell_text(cells, player_idx)

        if not raw_player or raw_player.lower() in ("player", "name", "totals", "team", "opponent", "opponents", "total", ""):
            continue

        player_name = raw_player.replace("*", "").strip()
        raw_jersey = cell_text(cells, num_idx) if num_idx is not None else ""
        jersey = jersey_clean(raw_jersey)

        row = {
            "number": jersey or "—",
            "player": player_name,
        }

        for (out_key, _) in HITTING_COLS:
            idx = col_indices.get(out_key)
            val = cell_text(cells, idx) if idx is not None else None
            row[out_key] = val if val not in (None, "", "-", "—") else "—"

        # Calculate OPS if missing but OBP & SLG% are present
        if row["OPS"] == "—" and obp_idx is not None and slg_idx is not None:
            try:
                obp = float(cell_text(cells, obp_idx))
                slg = float(cell_text(cells, slg_idx))
                row["OPS"] = f"{obp + slg:.3f}".lstrip("0")
            except Exception:
                pass

        rows.append(row)

    return rows


def parse_pitching_table(table) -> list[dict]:
    headers = header_cells(table)
    hdr_map = {norm_header(h): i for i, h in enumerate(headers)}

    num_idx = find_col(hdr_map, "##", "#", "No", "Num")
    player_idx = find_col(hdr_map, "Player", "Name")

    if player_idx is None:
        return []

    # Map target metrics
    col_indices = {
        out_key: find_col(hdr_map, *aliases)
        for (out_key, aliases) in PITCHING_COLS
    }

    # Backup columns to combine W and L if split into separate columns
    w_idx = find_col(hdr_map, "W", "Wins")
    l_idx = find_col(hdr_map, "L", "Losses")

    rows = []
    for tr in body_rows(table):
        cells = tr.find_all(["td", "th"])
        raw_player = cell_text(cells, player_idx)

        if not raw_player or raw_player.lower() in ("player", "name", "totals", "team", "opponent", "opponents", "total", ""):
            continue

        player_name = raw_player.replace("*", "").strip()
        raw_jersey = cell_text(cells, num_idx) if num_idx is not None else ""
        jersey = jersey_clean(raw_jersey)

        row = {
            "number": jersey or "—",
            "player": player_name,
        }

        for (out_key, _) in PITCHING_COLS:
            idx = col_indices.get(out_key)
            val = cell_text(cells, idx) if idx is not None else None
            row[out_key] = val if val not in (None, "", "-", "—") else "—"

        # Construct W-L if not in a single combined column
        if row["W-L"] == "—" and w_idx is not None and l_idx is not None:
            w_val = cell_text(cells, w_idx) or "0"
            l_val = cell_text(cells, l_idx) or "0"
            row["W-L"] = f"{w_val}-{l_val}"

        rows.append(row)

    return rows


def extract_table(html: str, stat_type: str) -> list[dict]:
    """Parse extracted HTML for hitting or pitching tables with explicit header validation."""
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")

    for table in tables:
        headers = header_cells(table)
        if not headers or not has_player_col(headers):
            continue

        if stat_type == "hitting" and is_hitting_table(headers):
            return parse_hitting_table(table)
        elif stat_type == "pitching" and is_pitching_table(headers):
            return parse_pitching_table(table)

    return []


def index_by_number(rows: list[dict]) -> dict:
    """Build a dict keyed by jersey number for rapid lookups."""
    indexed = {}
    for r in rows:
        key = r.get("number", "—")
        if not key or key == "—":
            key = r.get("player", "unknown")
        indexed[key] = r
    return indexed


# ── Playwright Tab / Category Switcher ───────────────────────────────────────

def switch_category(page, category_label: str) -> bool:
    """
    Switches to the requested category (e.g. Hitting, Pitching) by interacting
    with tabs, buttons, or Sidearm's dropdown.
    """
    labels = [category_label]
    if category_label.lower() in ("hitting", "batting"):
        labels = ["Hitting", "Batting"]
    elif category_label.lower() in ("pitching", "pitchers"):
        labels = ["Pitching", "Pitchers"]

    for label in labels:
        category_regex = re.compile(f"^{label}$", re.I)

        # 1. Try Tab / Button Click (Sidearm NextGen SPA)
        tab_locators = [
            page.locator("[role='tab']").filter(has_text=category_regex).first,
            page.locator("button").filter(has_text=category_regex).first,
            page.locator(f"button:has-text('{label}')").first,
            page.locator(f"a:has-text('{label}')").first,
        ]

        for tab in tab_locators:
            try:
                if tab.count() > 0 and tab.is_visible():
                    tab.click()
                    page.wait_for_timeout(800)
                    return True
            except Exception:
                continue

        # 2. Try Combobox / Select Dropdown Fallback
        combobox_selectors = [
            "button[role='combobox']",
            ".sidearm-common-stats-category-select button",
            "button[aria-haspopup='listbox']",
            "select[name*='category' i]",
        ]

        for sel in combobox_selectors:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                if sel.startswith("select"):
                    loc.select_option(label=label)
                    page.wait_for_timeout(800)
                    return True
                else:
                    loc.click()
                    page.wait_for_timeout(300)
                    opt = page.locator("li, [role='option']").filter(has_text=category_regex).first
                    if opt.count() > 0 and opt.is_visible():
                        opt.click()
                        page.wait_for_timeout(800)
                        return True

    return False


def fetch_all_stats(url: str) -> dict:
    from playwright.sync_api import sync_playwright

    print(f"  → Loading: {url}")
    hitting_data: list[dict] = []
    pitching_data: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 800})
        page = context.new_page()

        # Abort heavy assets (images/fonts) while keeping JS/XHR active
        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "media", "font")
            else route.continue_(),
        )

        page.goto(url, wait_until="domcontentloaded", timeout=45_000)

        # Wait for base stats container
        try:
            page.wait_for_selector("table tbody tr", timeout=20_000)
            print("  ✓ Base stats table detected.")
        except Exception:
            print("  ⚠ Timed out waiting for table selector.", file=sys.stderr)

        # ── 1. Batting / Hitting ─────────────────────────────────────────────
        print("  → Switching to Hitting / Batting view...")
        switch_category(page, "Hitting")
        try:
            page.wait_for_function(
                "() => Array.from(document.querySelectorAll('th, td')).some(el => /AVG|AB|RBI|SLG/i.test(el.innerText))",
                timeout=5_000,
            )
        except Exception:
            page.wait_for_timeout(1000)

        hitting_html = page.content()
        hitting_data = extract_table(hitting_html, "hitting")

        # ── 2. Pitching ──────────────────────────────────────────────────────
        print("  → Switching to Pitching view...")
        switch_category(page, "Pitching")
        try:
            page.wait_for_function(
                "() => Array.from(document.querySelectorAll('th, td')).some(el => /ERA|IP|WHIP/i.test(el.innerText))",
                timeout=6_000,
            )
        except Exception:
            page.wait_for_timeout(1200)

        pitching_html = page.content()
        pitching_data = extract_table(pitching_html, "pitching")

        browser.close()

    return {
        "hitting": hitting_data,
        "pitching": pitching_data,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Scrape St. Thomas baseball/softball stats.")
    ap.add_argument("--sport", default="baseball", choices=["baseball", "softball"],
                    help="Sport to scrape (default: baseball)")
    ap.add_argument("--year", default="2026",
                    help="Season year to scrape (default: 2026)")
    ap.add_argument("--out", default="docs/baseball_stats.json",
                    help="Output path for JSON (default: docs/baseball_stats.json)")
    args = ap.parse_args()

    out_path = Path(args.out)
    url = BASE_URL.format(sport=args.sport, year=args.year)
    team_name = f"University of St. Thomas Tommies {args.sport.capitalize()}"

    # Stale cache recovery check
    old_data: dict = {}
    if out_path.exists():
        try:
            old_data = json.loads(out_path.read_text(encoding="utf-8"))
            print(f"  ↩ Loaded previous data from {out_path} (stale fallback ready).")
        except Exception:
            pass

    try:
        parsed = fetch_all_stats(url)

        hitting_count = len(parsed["hitting"])
        pitching_count = len(parsed["pitching"])

        if hitting_count == 0 and pitching_count == 0:
            raise ValueError("No player rows parsed for hitting or pitching.")

        payload = {
            "team": team_name,
            "sport": args.sport,
            "season": args.year,
            "source": url,
            "status": "ok",
            "generated_at": now_iso(),
            "hitting": parsed["hitting"],
            "pitching": parsed["pitching"],
            "hitting_by_number": index_by_number(parsed["hitting"]),
            "pitching_by_number": index_by_number(parsed["pitching"]),
        }

        print(f"  ✓ Hitting:  {hitting_count} players parsed ({len(HITTING_COLS)} columns).")
        print(f"  ✓ Pitching: {pitching_count} players parsed ({len(PITCHING_COLS)} columns).")

    except Exception as exc:
        print(f"  ✗ Scrape failed: {exc}", file=sys.stderr)

        if old_data.get("hitting") or old_data.get("pitching"):
            print("  ↩ Keeping previous data as stale.")
            payload = {
                **old_data,
                "status": "stale",
                "error": str(exc)[:400],
                "generated_at": now_iso(),
            }
        else:
            payload = {
                "team": team_name,
                "sport": args.sport,
                "season": args.year,
                "source": url,
                "status": "error",
                "error": str(exc)[:400],
                "generated_at": now_iso(),
                "hitting": [],
                "pitching": [],
                "hitting_by_number": {},
                "pitching_by_number": {},
            }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\n✓ Written to {out_path}")


if __name__ == "__main__":
    main()
