#!/usr/bin/env python3
"""
scraper/scrape_baseball.py
Multi-school scraper for Sidearm Sports / Nuxt 3 baseball stats pages.
Fetches stats via Playwright and writes individual team JSON files + master JSON to docs/.

Usage:
  python scraper/scrape_baseball.py                 # Scrapes all schools (2026)
  python scraper/scrape_baseball.py --year 2026     # Specify year
  python scraper/scrape_baseball.py --school stthomas  # Scrape single school
"""

import argparse
import json
import os
import re
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ── Configured Sidearm Sports Schools ──────────────────────────────────────────
SCHOOLS = [
    {
        "id": "stthomas",
        "name": "University of St. Thomas Tommies",
        "url_template": "https://tommiesports.com/sports/baseball/stats/{year}",
        "out_file": "baseball_stats.json",  # primary default for backward compat
        "alias_file": "stthomas.json"
    },
    {
        "id": "sdstate",
        "name": "South Dakota State Jackrabbits",
        "url_template": "https://gojacks.com/sports/baseball/stats/{year}",
        "out_file": "sdstate.json"
    },
    {
        "id": "uncbears",
        "name": "Northern Colorado Bears",
        "url_template": "https://uncbears.com/sports/baseball/stats/{year}",
        "out_file": "uncbears.json"
    },
    {
        "id": "oru",
        "name": "Oral Roberts Golden Eagles",
        "url_template": "https://oruathletics.com/sports/baseball/stats/{year}",
        "out_file": "oru.json"
    },
    {
        "id": "omaha",
        "name": "Omaha Mavericks",
        "url_template": "https://omahamavs.com/sports/baseball/stats/{year}",
        "out_file": "omaha.json"
    }
]

DEFAULT_DOCS_DIR = "docs"


# ── Nuxt 3 devalue resolver ────────────────────────────────────────────────────
def resolve_nuxt_payload(raw_json_str):
    try:
        pool = json.loads(raw_json_str)
    except Exception as e:
        print(f"    [Error] JSON parse error: {e}")
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
            memo[val] = None
            res = resolve(target)
            memo[val] = res
            return res
        elif isinstance(val, list):
            return [resolve(x) for x in val]
        elif isinstance(val, dict):
            return {k: resolve(v) for k, v in val.items()}
        return val

    return [resolve(item) for item in pool]


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


# ── Fetch HTML via Playwright page ────────────────────────────────────────────
def fetch_html_with_page(page, url):
    print(f"  Navigating to: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)

    try:
        page.wait_for_selector("script#__NUXT_DATA__", timeout=25000)
        print("    __NUXT_DATA__ script detected.")
    except Exception:
        print("    Buffer 5s for client hydration...")
        page.wait_for_timeout(5000)

    return page.content()


# ── Extract stats from HTML ────────────────────────────────────────────────────
def extract_stats(html, school_meta, year):
    soup = BeautifulSoup(html, "html.parser")
    payload_str = None

    tag = soup.find("script", id="__NUXT_DATA__")
    if tag and tag.string:
        payload_str = tag.string.strip()
    else:
        for script in soup.find_all("script"):
            text = script.string or ""
            if "overallIndividualStats" in text or "battingAverage" in text:
                m = re.search(r"(\[.*\])", text, re.DOTALL)
                if m:
                    payload_str = m.group(1)
                    break

    if not payload_str:
        raise ValueError("Could not locate Nuxt JSON payload in page source.")

    resolved_pool = resolve_nuxt_payload(payload_str)
    if not resolved_pool:
        raise ValueError("Failed to parse and resolve Nuxt JSON data.")

    stat_lists = find_all_records_by_key(resolved_pool, "overallIndividualStats")

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

        # Batting
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

        # Pitching
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

    hitting_list.sort(key=lambda x: x.pop("_sort"))
    pitching_list.sort(key=lambda x: x.pop("_sort"))
    players_sorted = [players_map[k] for k in sorted(players_map.keys())]

    target_url = school_meta["url_template"].format(year=year)

    return {
        "team_id": school_meta["id"],
        "team": school_meta["name"],
        "sport": "baseball",
        "season": year,
        "source": target_url,
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
    parser = argparse.ArgumentParser(description="Multi-school Sidearm Baseball Stats Scraper")
    parser.add_argument("--year", default="2026", help="Season year (e.g. 2026)")
    parser.add_argument("--docs-dir", default=DEFAULT_DOCS_DIR, help="Destination directory for output JSON files")
    parser.add_argument("--school", default=None, help="Scrape only one school ID (e.g. stthomas, sdstate, uncbears, oru, omaha)")
    args = parser.parse_args()

    os.makedirs(args.docs_dir, exist_ok=True)

    schools_to_scrape = SCHOOLS
    if args.school:
        schools_to_scrape = [s for s in SCHOOLS if s["id"] == args.school.lower()]
        if not schools_to_scrape:
            print(f"Unknown school ID '{args.school}'. Available: {[s['id'] for s in SCHOOLS]}")
            return

    print(f"Starting scrape for {len(schools_to_scrape)} school(s) [Year: {args.year}]...")

    all_teams_data = {}

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

        for school in schools_to_scrape:
            target_url = school["url_template"].format(year=args.year)
            print(f"\n▶ [{school['id'].upper()}] {school['name']}")
            try:
                html = fetch_html_with_page(page, target_url)
                stats = extract_stats(html, school, args.year)

                out_path = os.path.join(args.docs_dir, school["out_file"])
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(stats, f, indent=2)

                # Write alias copy if defined (e.g. stthomas.json in addition to baseball_stats.json)
                if "alias_file" in school:
                    alias_path = os.path.join(args.docs_dir, school["alias_file"])
                    with open(alias_path, "w", encoding="utf-8") as f:
                        json.dump(stats, f, indent=2)

                all_teams_data[school["id"]] = stats

                h = len(stats["hitting"])
                p_cnt = len(stats["pitching"])
                print(f"  ✓ Saved: {h} batters, {p_cnt} pitchers → {out_path}")

            except Exception as e:
                print(f"  ✗ Failed for {school['name']}: {e}")

        browser.close()

    # Save aggregated conference file if scraping multiple schools
    if len(schools_to_scrape) > 1:
        master_file = os.path.join(args.docs_dir, "all_teams_stats.json")
        with open(master_file, "w", encoding="utf-8") as f:
            json.dump({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "season": args.year,
                "teams": all_teams_data
            }, f, indent=2)
        print(f"\n★ Master file saved: {master_file} ({len(all_teams_data)} teams)")

    print("\nAll done!")


if __name__ == "__main__":
    main()
