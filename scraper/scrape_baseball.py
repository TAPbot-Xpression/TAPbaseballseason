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
    """
    Nuxt 3 devalue parser.
    The payload is a list where numbers are index pointers to other list items.
    """
    try:
        pool = json.loads(raw_json_str)
    except Exception as e:
        print(f"  JSON parse error: {e}")
        return None

    if not isinstance(pool, list):
        return pool

    memo = {}

    def resolve(val):
        if isinstance(val, int):
            if val < 0 or val >= len(pool):
                return val
            if val in memo:
                return memo[val]
            target = pool[val]
            memo[val] = None  # prevent infinite recursion
            res = resolve(target)
            memo[val] = res
            return res
        elif isinstance(val, list):
            return [resolve(x) for x in val]
        elif isinstance(val, dict):
            return {k: resolve(v) for k, v in val.items()}
        return val

    # Resolve all objects in the pool to ensure nothing is missed
    resolved_pool = [resolve(item) for item in pool]
    return resolved_pool


# ── Recursive search in resolved data ──────────────────────────────────────────
def find_all_records_by_key(obj, target_key, found=None):
    if found is None:
        found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == target_key and isinstance(v, list):
                found.append(v)
            else:
                find_all_records_by_key(v, target_key, found)
    elif isinstance(obj, list):
        for item in obj:
            find_all_records_by_key(item, target_key, found)
    return found


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
        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        # Wait for the Nuxt data script or fallback buffer
        try:
            page.wait_for_selector("script#__NUXT_DATA__", timeout=25000)
            print("  __NUXT_DATA__ script tag detected.")
        except Exception:
            print("  Waiting 5s for client hydration...")
            page.wait_for_timeout(5000)

        page.screenshot(path="debug_before_click.png", full_page=True)
        print("  Screenshot saved: debug_before_click.png")

        html = page.content()
        browser.close()
        return html


# ── Extract stats from HTML ────────────────────────────────────────────────────
def extract_stats(html, year):
    soup = BeautifulSoup(html, "html.parser")
    payload_str = None

    # 1. Nuxt 3 data island
    tag = soup.find("script", id="__NUXT_DATA__")
    if tag and tag.string:
        payload_str = tag.string.strip()
        print("  Found __NUXT_DATA__ script.")

    # 2. Inline fallback
    if not payload_str:
        for script in soup.find_all("script"):
            text = script.string or ""
            if "overallIndividualStats" in text or "battingAverage" in text:
                m = re.search(r"(\[.*\])", text, re.DOTALL)
                if m:
                    payload_str = m.group(1)
                    print("  Found inline JSON payload.")
                    break

    if not payload_str:
        raise ValueError("Could not locate Nuxt JSON data in page source.")

    resolved_pool = resolve_nuxt_payload(payload_str)
    if not resolved_pool:
        raise ValueError("Failed to parse and resolve Nuxt JSON data.")

    # Search for stats lists
    stat_lists = find_all_records_by_key(resolved_pool, "overallIndividualStats")
    print(f"  Found {len(stat_lists)} 'overallIndividualStats' block(s).")

    # Fallback to search any list containing dicts with 'playerUniform'
    if not stat_lists:
        def find_player_lists(obj, acc):
            if isinstance(obj, list):
                if any(isinstance(x, dict) and "playerUniform" in x for x in obj):
                    acc.append(obj)
                for item in obj:
                    find_player_lists(item, acc)
            elif isinstance(obj, dict):
                for v in obj.values():
                    find_player_lists(v, acc)
        stat_lists = []
        find_player_lists(resolved_pool, stat_lists)
        print(f"  Fallback: Found {len(stat_lists)} lists with playerUniform.")

    # Flatten and deduplicate entries
    raw_entries = []
    for slist in stat_lists:
        for entry in slist:
            if isinstance(entry, dict):
                raw_entries.append(entry)

    def clean_val(v, default="0"):
        if v is None or v == -1 or str(v).strip() in ("-1", "", "None"):
            return default
        return str(v).strip()

    def parse_float(v):
        try:
            return float(str(v).replace("-", "0"))
        except (ValueError, TypeError):
            return 0.0

    hitting_list = []
    pitching_list = []
    hitting_by_num = {}
    pitching_by_num = {}
    players_map = {}

    for entry in raw_entries:
        if entry.get("isAFooterStat") == 1:
            continue

        raw_num = str(entry.get("playerUniform", "")).strip()
        name = str(entry.get("playerName", entry.get("name", ""))).strip()

        if not raw_num or not name or raw_num == "-1" or name == "-1":
            continue

        try:
            jersey_int = int(raw_num)
        except ValueError:
            jersey_int = 999

        # Batting Fields
        avg = clean_val(entry.get("battingAverage"), ".000")
        ab  = clean_val(entry.get("atBats"), "0")
        r   = clean_val(entry.get("runs"), "0")
        h   = clean_val(entry.get("hits"), "0")
        d2  = clean_val(entry.get("doubles"), "0")
        d3  = clean_val(entry.get("triples"), "0")
        hr  = clean_val(entry.get("homeRuns"), "0")
        rbi = clean_val(entry.get("runsBattedIn"), "0")
        ops = clean_val(entry.get("ops"), ".000")
        slg = clean_val(entry.get("sluggingPercentage"), ".000")

        # Pitching Fields
        era  = clean_val(entry.get("earnedRunAverage"), "-")
        whip = clean_val(entry.get("whip"), "-")
        wins = clean_val(entry.get("wins"), "0")
        loss = clean_val(entry.get("losses"), "0")
        ip   = clean_val(entry.get("inningsPitched"), "0.0")

        so_raw = (
            entry.get("strikeoutsPitching")
            or entry.get("strikeoutsAllowed")
            or entry.get("pitchingStrikeouts")
            or entry.get("strikeouts")
        )
        so = clean_val(so_raw, "0")
        wl = f"{wins}-{loss}"

        # Detection logic
        has_batting = (
            parse_float(ab) > 0
            or parse_float(h) > 0
            or parse_float(r) > 0
            or (avg != ".000" and avg != "0" and avg != "-")
        )

        has_pitching = (
            parse_float(ip) > 0
            or era != "-"
            or parse_float(wins) > 0
            or parse_float(loss) > 0
            or (whip != "-" and whip != "0")
        )

        # Build Hitting item
        if has_batting and raw_num not in hitting_by_num:
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
                "_sort": jersey_int
            }
            hitting_list.append(bat)
            hitting_by_num[raw_num] = {k: v for k, v in bat.items() if k != "_sort"}

        # Build Pitching item
        if has_pitching and raw_num not in pitching_by_num:
            pit = {
                "number": raw_num,
                "player": name,
                "ERA": era,
                "WHIP": whip,
                "W-L": wl,
                "IP": ip,
                "SO": so,
                "_sort": jersey_int
            }
            pitching_list.append(pit)
            pitching_by_num[raw_num] = {k: v for k, v in pit.items() if k != "_sort"}

        # Combined map
        if has_batting or has_pitching:
            p = players_map.get(jersey_int, {
                "jersey_number": jersey_int,
                "player_name": name
            })
            if has_batting:
                p["batting_stats"] = {
                    "AVG": avg, "R": r, "H": h, "2B": d2, "3B": d3,
                    "HR": hr, "RBI": rbi, "OPS": ops, "SLG%": slg
                }
            if has_pitching:
                p["pitching_stats"] = {
                    "ERA": era, "WHIP": whip, "W-L": wl, "IP": ip, "SO": so
                }
            players_map[jersey_int] = p

    # Sort by Jersey Number
    hitting_list.sort(key=lambda x: x.pop("_sort"))
    pitching_list.sort(key=lambda x: x.pop("_sort"))
    players_sorted = [players_map[k] for k in sorted(players_map.keys())]

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
    tot = len(result["players"])
    print(f"Done — {h} batters, {p} pitchers ({tot} total players) → {args.out}")


if __name__ == "__main__":
    main()
