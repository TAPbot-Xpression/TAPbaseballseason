#!/usr/bin/env python3
"""
scraper/scrape_baseball.py
Multi-school scraper for Sidearm Sports baseball stats pages.
Supports both modern Nuxt 3 (JSON payload) and Classic ASP.NET / Knockout (HTML table) Sidearm layouts.

Usage:
  python scraper/scrape_baseball.py                 # Scrapes all schools (2026)
  python scraper/scrape_baseball.py --docs-dir docs --year 2026
"""

import argparse
import json
import os
import re
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ── Configured Schools ────────────────────────────────────────────────────────
SCHOOLS = [
    {
        "id": "stthomas",
        "name": "University of St. Thomas Tommies",
        "url_template": "https://tommiesports.com/sports/baseball/stats/{year}",
        "out_file": "baseball_stats.json",
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


def clean_val(v, default="0"):
    if v is None or v == -1 or str(v).strip() in ("-1", "", "None", "--"):
        return default
    return str(v).strip()


def parse_float(v):
    try:
        return float(str(v).replace("-", "0").replace("\xa0", "").strip())
    except (ValueError, TypeError):
        return 0.0


# ── Method 1: Extract from Nuxt 3 Payload ──────────────────────────────────────
def try_extract_nuxt(soup):
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
        return None

    resolved_pool = resolve_nuxt_payload(payload_str)
    if not resolved_pool:
        return None

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

    if not raw_entries:
        return None

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
                "number": raw_num, "player": name, "AVG": avg, "R": r, "H": h,
                "2B": d2, "3B": d3, "HR": hr, "RBI": rbi, "OPS": ops, "SLG%": slg,
                "_sort": jersey_int
            }
            hitting_list.append(bat)
            hitting_by_num[raw_num] = {k: v for k, v in bat.items() if k != "_sort"}

        if has_pitching and raw_num not in pitching_by_num:
            pit = {
                "number": raw_num, "player": name, "ERA": era, "WHIP": whip,
                "W-L": wl, "IP": ip, "SO": so, "_sort": jersey_int
            }
            pitching_list.append(pit)
            pitching_by_num[raw_num] = {k: v for k, v in pit.items() if k != "_sort"}

        if has_batting or has_pitching:
            p = players_map.get(jersey_int, {"jersey_number": jersey_int, "player_name": name})
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

    return hitting_list, pitching_list, hitting_by_num, pitching_by_num, players_map


# ── Method 2: Extract from Classic Sidearm HTML Tables (Fallback for ORU) ────
def try_extract_sidearm_html(soup):
    def get_cell(tr, label, default="0"):
        td = tr.find(lambda tag: tag.name in ["td", "th"] and tag.get("data-label", "").lower() == label.lower())
        if td:
            return clean_val(td.get_text(" ", strip=True), default)
        return default

    def extract_jersey_and_name(tr):
        # Look for mobile jersey span first
        num_span = tr.find("span", class_="mobile-jersey-number")
        raw_num = num_span.get_text(strip=True) if num_span else ""

        # Fallback to jersey TD
        if not raw_num:
            num_td = tr.find("td", class_=lambda c: c and "hide-on-medium-down" in c)
            if num_td:
                raw_num = num_td.get_text(strip=True)

        # Extract Player Name
        name_tag = tr.find("a", attrs={"data-player-id": True}) or tr.find("th", scope="row") or tr.find("a")
        raw_name = ""
        if name_tag:
            # Strip out mobile jersey span text if nested
            text = name_tag.get_text(" ", strip=True)
            if raw_num and text.startswith(raw_num):
                text = text[len(raw_num):].strip()
            raw_name = text.strip()

        return raw_num, raw_name

    hitting_list = []
    pitching_list = []
    hitting_by_num = {}
    pitching_by_num = {}
    players_map = {}

    # 1. Parse Batting Section
    batting_section = soup.find(id="individual-overall-batting") or soup.find(id="overall-batting")
    batting_table = batting_section.find("table") if batting_section else soup.find("table", summary=lambda s: s and "batting" in s.lower())
    
    if not batting_table:
        for t in soup.find_all("table"):
            cap = t.find("caption")
            if cap and "batting" in cap.get_text().lower() and "overall" in cap.get_text().lower():
                batting_table = t
                break

    if batting_table:
        tbody = batting_table.find("tbody") or batting_table
        for tr in tbody.find_all("tr"):
            raw_num, name = extract_jersey_and_name(tr)
            if not raw_num or not name or name.lower() in ["totals", "opponents"]:
                continue

            try:
                jersey_int = int(raw_num)
            except ValueError:
                jersey_int = 999

            avg = get_cell(tr, "AVG", ".000")
            ab  = get_cell(tr, "AB", "0")
            r   = get_cell(tr, "R", "0")
            h   = get_cell(tr, "H", "0")
            d2  = get_cell(tr, "2B", "0")
            d3  = get_cell(tr, "3B", "0")
            hr  = get_cell(tr, "HR", "0")
            rbi = get_cell(tr, "RBI", "0")
            ops = get_cell(tr, "OPS", ".000")
            slg = get_cell(tr, "SLG%", ".000")

            if parse_float(ab) > 0 or parse_float(h) > 0 or parse_float(r) > 0:
                bat = {
                    "number": raw_num, "player": name, "AVG": avg, "R": r, "H": h,
                    "2B": d2, "3B": d3, "HR": hr, "RBI": rbi, "OPS": ops, "SLG%": slg,
                    "_sort": jersey_int
                }
                hitting_list.append(bat)
                hitting_by_num[raw_num] = {k: v for k, v in bat.items() if k != "_sort"}

                p = players_map.get(jersey_int, {"jersey_number": jersey_int, "player_name": name})
                p["batting_stats"] = {
                    "AVG": avg, "R": r, "H": h, "2B": d2, "3B": d3,
                    "HR": hr, "RBI": rbi, "OPS": ops, "SLG%": slg
                }
                players_map[jersey_int] = p

    # 2. Parse Pitching Section
    pitching_section = soup.find(id="individual-overall-pitching") or soup.find(id="overall-pitching")
    pitching_table = pitching_section.find("table") if pitching_section else None

    if not pitching_table:
        for t in soup.find_all("table"):
            cap = t.find("caption")
            if cap and "pitching" in cap.get_text().lower() and "overall" in cap.get_text().lower():
                pitching_table = t
                break

    if pitching_table:
        tbody = pitching_table.find("tbody") or pitching_table
        for tr in tbody.find_all("tr"):
            raw_num, name = extract_jersey_and_name(tr)
            if not raw_num or not name or name.lower() in ["totals", "opponents"]:
                continue

            try:
                jersey_int = int(raw_num)
            except ValueError:
                jersey_int = 999

            era  = get_cell(tr, "ERA", "-")
            whip = get_cell(tr, "WHIP", "-")
            wl   = get_cell(tr, "W-L", "0-0")
            ip   = get_cell(tr, "IP", "0.0")
            so   = get_cell(tr, "SO", "0")

            if parse_float(ip) > 0 or era != "-":
                pit = {
                    "number": raw_num, "player": name, "ERA": era, "WHIP": whip,
                    "W-L": wl, "IP": ip, "SO": so, "_sort": jersey_int
                }
                pitching_list.append(pit)
                pitching_by_num[raw_num] = {k: v for k, v in pit.items() if k != "_sort"}

                p = players_map.get(jersey_int, {"jersey_number": jersey_int, "player_name": name})
                p["pitching_stats"] = {
                    "ERA": era, "WHIP": whip, "W-L": wl, "IP": ip, "SO": so
                }
                players_map[jersey_int] = p

    return hitting_list, pitching_list, hitting_by_num, pitching_by_num, players_map


# ── Unified Extractor ─────────────────────────────────────────────────────────
def extract_stats(html, school_meta, year):
    soup = BeautifulSoup(html, "html.parser")

    # Try Nuxt 3 parser first
    res = try_extract_nuxt(soup)
    if res and (res[0] or res[1]):
        print("    [Format] Nuxt 3 JSON payload parsed successfully.")
        hitting_list, pitching_list, hitting_by_num, pitching_by_num, players_map = res
    else:
        # Fall back to Sidearm Classic ASP.NET HTML tables (e.g. ORU)
        print("    [Format] Nuxt payload not found. Falling back to Sidearm HTML table parser...")
        hitting_list, pitching_list, hitting_by_num, pitching_by_num, players_map = try_extract_sidearm_html(soup)

    if not hitting_list and not pitching_list:
        raise ValueError(f"Could not extract batting or pitching stats for {school_meta['name']}.")

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


# ── Playwright Fetcher ─────────────────────────────────────────────────────────
def fetch_html_with_page(page, url):
    print(f"  Navigating to: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)

    try:
        # Wait briefly for Nuxt or Sidearm table
        page.wait_for_selector("script#__NUXT_DATA__, table.sidearm-table", timeout=20000)
    except Exception:
        page.wait_for_timeout(4000)

    return page.content()


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Multi-school Sidearm Baseball Stats Scraper")
    parser.add_argument("--year", default="2026", help="Season year (e.g. 2026)")
    parser.add_argument("--docs-dir", default=DEFAULT_DOCS_DIR, help="Destination directory for output JSON files")
    parser.add_argument("--school", default=None, help="Scrape only one school ID")
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
