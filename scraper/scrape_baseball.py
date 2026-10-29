#!/usr/bin/env python3
"""
scrape.py — Tommie Baseball Stats Scraper
Fetches live stats from https://tommiesports.com/sports/baseball/stats/2026
and extracts Batting and Pitching statistics from the embedded Nuxt data payload.
"""

import json
import re
import sys
import urllib.request
from datetime import datetime
from bs4 import BeautifulSoup

URL = "https://tommiesports.com/sports/baseball/stats/2026"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_html(url):
    """Fetches HTML page content with custom headers."""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as response:
        return response.read().decode("utf-8")


def parse_devalue_payload(raw_json_str):
    """
    Nuxt 3 devalue format: Root array where elements reference other array indices.
    This resolves the index pointers back into standard Python dicts/lists/values.
    """
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
            target = pool[val]
            memo[val] = None  # Guard against circular references
            resolved_target = resolve(target)
            memo[val] = resolved_target
            return resolved_target

        elif isinstance(val, list):
            return [resolve(x) for x in val]

        elif isinstance(val, dict):
            return {k: resolve(v) for k, v in val.items()}

        return val

    # Resolved root state
    return resolve(pool[0] if len(pool) > 0 else {})


def find_key_recursive(obj, target_key):
    """Recursively traverses dictionaries/lists to find a target key."""
    if isinstance(obj, dict):
        if target_key in obj:
            return obj[target_key]
        for v in obj.values():
            res = find_key_recursive(v, target_key)
            if res is not None:
                return res
    elif isinstance(obj, list):
        for item in obj:
            res = find_key_recursive(item, target_key)
            if res is not None:
                return res
    return None


def extract_stats(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    payload_str = None

    # 1. Look for Nuxt 3 data island tag (<script id="__NUXT_DATA__">)
    nuxt_data_tag = soup.find("script", id="__NUXT_DATA__")
    if nuxt_data_tag and nuxt_data_tag.string:
        payload_str = nuxt_data_tag.string.strip()

    # 2. Fallback to inline script containing __NUXT__ or stats definitions
    if not payload_str:
        for script in soup.find_all("script"):
            text = script.string or ""
            if "overallIndividualStats" in text or "__NUXT__" in text:
                match = re.search(r"(\[.*\])", text, re.DOTALL)
                if match:
                    payload_str = match.group(1)
                    break

    if not payload_str:
        raise ValueError("Could not find embedded Nuxt JSON state in the page source.")

    # Resolve Nuxt payload structure
    resolved_data = parse_devalue_payload(payload_str)
    individual_stats = find_key_recursive(resolved_data, "overallIndividualStats")

    if not individual_stats or not isinstance(individual_stats, list):
        raise ValueError("Could not locate 'overallIndividualStats' within Nuxt payload.")

    def clean_val(v, default="0"):
        if v is None or v == -1 or v == "-1" or str(v).strip() == "":
            return default
        return str(v).strip()

    players_by_jersey = {}
    hitting_list = []
    pitching_list = []
    hitting_by_num = {}
    pitching_by_num = {}

    for entry in individual_stats:
        if not isinstance(entry, dict) or entry.get("isAFooterStat") == 1:
            continue

        raw_jersey = str(entry.get("playerUniform", "")).strip()
        player_name = str(entry.get("playerName", "")).strip()

        if not raw_jersey or not player_name or player_name == "-1":
            continue

        try:
            jersey_int = int(raw_jersey)
            jersey_str = str(jersey_int)
        except ValueError:
            jersey_int = 999
            jersey_str = raw_jersey

        # --- Batting Stats (AVG, R, H, 2B, 3B, HR, RBI, OPS, SLG%) ---
        avg = clean_val(entry.get("battingAverage"), ".000")
        ab  = clean_val(entry.get("atBats"), "0")
        r   = clean_val(entry.get("runs"), "0")
        h   = clean_val(entry.get("hits"), "0")
        d2b = clean_val(entry.get("doubles"), "0")
        d3b = clean_val(entry.get("triples"), "0")
        hr  = clean_val(entry.get("homeRuns"), "0")
        rbi = clean_val(entry.get("runsBattedIn"), "0")
        ops = clean_val(entry.get("ops"), ".000")
        slg = clean_val(entry.get("sluggingPercentage"), ".000")

        # --- Pitching Stats (ERA, WHIP, W-L, IP, SO) ---
        era   = clean_val(entry.get("earnedRunAverage"), "-")
        whip  = clean_val(entry.get("whip"), "-")
        wins  = clean_val(entry.get("wins"), "0")
        loss  = clean_val(entry.get("losses"), "0")
        ip    = clean_val(entry.get("inningsPitched"), "0.0")
        so    = clean_val(entry.get("strikeoutsPitching", entry.get("strikeouts")), "0")
        wl    = f"{wins}-{loss}"

        has_batting  = int(ab) > 0 or r != "0" or h != "0"
        has_pitching = era != "-" or ip != "0.0" or int(wins) > 0 or int(loss) > 0

        # Batting item
        if has_batting:
            bat_item = {
                "number": jersey_str,
                "jersey_num": jersey_int,
                "player": player_name,
                "AVG": avg,
                "R": r,
                "H": h,
                "2B": d2b,
                "3B": d3b,
                "HR": hr,
                "RBI": rbi,
                "OPS": ops,
                "SLG%": slg
            }
            hitting_list.append(bat_item)
            hitting_by_num[jersey_str] = bat_item

        # Pitching item
        if has_pitching:
            pitch_item = {
                "number": jersey_str,
                "jersey_num": jersey_int,
                "player": player_name,
                "ERA": era,
                "WHIP": whip,
                "W-L": wl,
                "IP": ip,
                "SO": so
            }
            pitching_list.append(pitch_item)
            pitching_by_num[jersey_str] = pitch_item

        # Combined player object
        if has_batting or has_pitching:
            p_obj = {
                "jersey_number": jersey_int,
                "player_name": player_name
            }
            if has_batting:
                p_obj["batting_stats"] = {
                    "AVG": avg, "R": r, "H": h, "2B": d2b, "3B": d3b,
                    "HR": hr, "RBI": rbi, "OPS": ops, "SLG%": slg
                }
            if has_pitching:
                p_obj["pitching_stats"] = {
                    "ERA": era, "WHIP": whip, "W-L": wl, "IP": ip, "SO": so
                }
            players_by_jersey[jersey_int] = p_obj

    # Sort all lists by jersey number ascending
    hitting_list.sort(key=lambda x: x["jersey_num"])
    pitching_list.sort(key=lambda x: x["jersey_num"])
    all_players_sorted = [players_by_jersey[k] for k in sorted(players_by_jersey.keys())]

    # Clean internal sorting key before output
    for item in hitting_list:
        item.pop("jersey_num", None)
    for item in pitching_list:
        item.pop("jersey_num", None)

    return {
        "status": "ok",
        "season": "2026",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "players": all_players_sorted,
        "hitting": hitting_list,
        "pitching": pitching_list,
        "hitting_by_number": hitting_by_num,
        "pitching_by_number": pitching_by_num
    }


def main():
    target_url = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].startswith("http") else URL
    output_file = sys.argv[2] if len(sys.argv) > 2 else "baseball_stats.json"

    print(f"Fetching page source from: {target_url}...")
    try:
        html = fetch_html(target_url)
    except Exception as e:
        print(f"Failed to fetch {target_url}: {e}")
        return

    print("Parsing Nuxt JSON state...")
    try:
        data = extract_stats(html)
    except Exception as e:
        print(f"Error extracting data: {e}")
        return

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"Successfully saved stats for {len(data['players'])} players to '{output_file}' (sorted by jersey number).")


if __name__ == "__main__":
    main()
