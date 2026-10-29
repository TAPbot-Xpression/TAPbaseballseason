#!/usr/bin/env python3
"""
scraper/scrape_baseball.py
Fetches Tommie Baseball stats using Playwright (headless Chromium),
resolves the Nuxt 3 devalue JSON payload, and writes to --out path.

Usage:
  python scraper/scrape_baseball.py --out docs/baseball_stats.json --year 2026
"""

import argparse
import json
import os
import re
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

DEFAULT_URL = "https://tommiesports.com/sports/baseball/stats/2026"
DEFAULT_OUT = "docs/baseball_stats.json"
DEFAULT_YEAR = "2026"


# ── Nuxt 3 devalue resolver ────────────────────────────────────────────────────
def resolve_nuxt_payload(raw_json_str):
    pool = json.loads(raw_json_str)
    if not isinstance(pool, list):
        return pool

    memo = {}

    def resolve(val):
        if isinstance(val, int):
            if val < 0 or val >= len(pool):
                return val
            if val in memo:
                return memo[val]
            memo[val] = None
            result = resolve(pool[val])
            memo[val] = result
            return result
        if isinstance(val, list):
            return [resolve(x) for x in val]
        if isinstance(val, dict):
            return {k: resolve(v) for k, v in val.items()}
        return val

    return resolve(pool[0] if pool else {})


# ── Recursive key finders ──────────────────────────────────────────────────────
def find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = find_key(item, key)
            if r is not None:
                return r
    return None


def find_all_key(obj, key, results=None):
    if results is None:
        results = []
    if isinstance(obj, dict):
        if key in obj:
            results.append(obj[key])
        for v in obj.values():
            find_all_key(v, key, results)
    elif isinstance(obj, list):
        for item in obj:
            find_all_key(item, key, results)
    return results


# ── Fetch HTML via Playwright ──────────────────────────────────────────────────
def fetch_html_playwright(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        print(f"  Navigating to {url} ...")

        # Use domcontentloaded instead of networkidle to prevent timeout from background analytics/ads
        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        # Wait specifically for the Nuxt data payload script to be loaded
        try:
            page.wait_for_selector("script#__NUXT_DATA__", timeout=30000)
            print("  __NUXT_DATA__ script tag found.")
        except Exception:
            print("  WARNING: script#__NUXT_DATA__ not found via selector — allowing hydration buffer.")
            page.wait_for_timeout(5000)

        # Save debug screenshot for GitHub Actions artifact upload
        page.screenshot(path="debug_before_click.png", full_page=True)
        print("  Screenshot saved: debug_before_click.png")

        html = page.content()
        browser.close()
        return html


# ── Extract stats from HTML ────────────────────────────────────────────────────
def extract_stats(html, year):
    soup = BeautifulSoup(html, "html.parser")
    payload_str = None

    # 1. Nuxt 3 data island <script id="__NUXT_DATA__">
    tag = soup.find("script", id="__NUXT_DATA__")
    if tag and tag.string:
        payload_str = tag.string.strip()
        print("  Found __NUXT_DATA__ script tag.")

    # 2. Inline window.__NUXT__ fallback
    if not payload_str:
        for script in soup.find_all("script"):
            text = script.string or ""
            if "overallIndividualStats" in text or "__NUXT__" in text:
                m = re.search(r"(\[.*\])", text, re.DOTALL)
                if m:
                    payload_str = m.group(1)
                    print("  Found inline __NUXT__ payload.")
                    break

    if not payload_str:
        raise ValueError(
            "Could not locate Nuxt JSON payload in page source. "
            "Check debug_before_click.png for what the page rendered."
        )

    data = resolve_nuxt_payload(payload_str)

    all_stat_lists = find_all_key(data, "overallIndividualStats")
    print(f"  Found {len(all_stat_lists)} overallIndividualStats node(s).")

    # Deduplicate entries across all stat nodes
    seen = set()
    entries = []
    for stat_list in all_stat_lists:
        if not isinstance(stat_list, list):
            continue
        for entry in stat_list:
            if not isinstance(entry, dict):
                continue
            uid = (str(entry.get("playerName", "")), str(entry.get("playerUniform", "")))
            if uid not in seen:
                seen.add(uid)
                entries.append(entry)

    print(f"  Total unique player entries: {len(entries)}")

    def clean(v, default="0"):
        if v is None or v == -1 or str(v).strip() in ("-1", ""):
            return default
        return str(v).strip()

    def is_valid(v):
        return v not in (None, -1, "-1", "", "0", 0)

    hitting_list = []
    pitching_list = []
    hitting_by_num = {}
    pitching_by_num = {}
    players_map = {}

    for entry in entries:
        if entry.get("isAFooterStat") == 1:
            continue

        raw_num = str(entry.get("playerUniform", "")).strip()
        name = str(entry.get("playerName", "")).strip()
        if not raw_num or not name or name == "-1":
            continue

        try:
            jersey_int = int(raw_num)
        except ValueError:
            jersey_int = 999

        # Batting
        avg = clean(entry.get("battingAverage"), ".000")
        ab = clean(entry.get("atBats"), "0")
        r = clean(entry.get("runs"), "0")
        h = clean(entry.get("hits"), "0")
        d2 = clean(entry.get("doubles"), "0")
        d3 = clean(entry.get("triples"), "0")
        hr = clean(entry.get("homeRuns"), "0")
        rbi = clean(entry.get("runsBattedIn"), "0")
        ops = clean(entry.get("ops"), ".000")
        slg = clean(entry.get("sluggingPercentage"), ".000")

        has_batting = (
            is_valid(entry.get("atBats"))
            or is_valid(entry.get("hits"))
            or is_valid(entry.get("runs"))
        )

        # Pitching
        era = clean(entry.get("earnedRunAverage"), "-")
        whip = clean(entry.get("whip"), "-")
        wins = clean(entry.get("wins"), "0")
        loss = clean(entry.get("losses"), "0")
        ip = clean(entry.get("inningsPitched"), "0.0")
        so_raw = (
            entry.get("strikeoutsPitching")
            or entry.get("strikeoutsAllowed")
            or entry.get("pitchingStrikeouts")
            or entry.get("strikeouts")
        )
        so = clean(so_raw, "0")
        wl = f"{wins}-{loss}"

        has_pitching = (
            is_valid(entry.get("earnedRunAverage"))
            or is_valid(entry.get("inningsPitched"))
            or is_valid(entry.get("wins"))
            or is_valid(entry.get("losses"))
            or is_valid(entry.get("whip"))
        )

        if has_batting:
            bat = {
                "number": raw_num,
                "player": name,
                "AVG": avg,
                "R": r,
                "H": h,
                "2B": d2,
                "3B": d3,
                "HR": hr,
                "RBI": rbi,
                "OPS": ops,
                "SLG%": slg,
                "_sort": jersey_int,
            }
            hitting_list.append(bat)
            hitting_by_num[raw_num] = {k: v for k, v in bat.items() if k != "_sort"}

        if has_pitching:
            pit = {
                "number": raw_num,
                "player": name,
                "ERA": era,
                "WHIP": whip,
                "W-L": wl,
                "IP": ip,
                "SO": so,
                "_sort": jersey_int,
            }
            pitching_list.append(pit)
            pitching_by_num[raw_num] = {k: v for k, v in pit.items() if k != "_sort"}

        if has_batting or has_pitching:
            p = players_map.get(
                jersey_int,
                {"jersey_number": jersey_int, "player_name": name},
            )
            if has_batting:
                p["batting_stats"] = {
                    "AVG": avg,
                    "R": r,
                    "H": h,
                    "2B": d2,
                    "3B": d3,
                    "HR": hr,
                    "RBI": rbi,
                    "OPS": ops,
                    "SLG%": slg,
                }
            if has_pitching:
                p["pitching_stats"] = {
                    "ERA": era,
                    "WHIP": whip,
                    "W-L": wl,
                    "IP": ip,
                    "SO": so,
                }
            players_map[jersey_int] = p

    hitting_list.sort(key=lambda x: x.pop("_sort"))
    pitching_list.sort(key=lambda x: x.pop("_sort"))
    players_sorted = [players_map[k] for k in sorted(players_map)]

    return {
        "team": "University of St. Thomas Tommies Baseball",
        "sport": "baseball",
        "season": year,
        "source": DEFAULT_URL,
        "status": "ok",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "players": players_sorted,
        "hitting": hitting_list,
        "pitching": pitching_list,
        "hitting_by_number": hitting_by_num,
        "pitching_by_number": pitching_by_num,
    }


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Scrape Tommie Baseball Stats")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output JSON path")
    parser.add_argument("--year", default=DEFAULT_YEAR, help="Season year")
    parser.add_argument("--url", default=DEFAULT_URL, help="Stats page URL")
    args = parser.parse_args()

    url = args.url.replace("2026", args.year) if args.year != "2026" else args.url

    print(f"Fetching (Playwright): {url}")
    html = fetch_html_playwright(url)

    print("Parsing Nuxt payload...")
    result = extract_stats(html, args.year)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    h = len(result["hitting"])
    p = len(result["pitching"])
    print(f"Done — {h} batters, {p} pitchers → {args.out}")


if __name__ == "__main__":
    main()
