#!/usr/bin/env python3
"""
scraper/scrape_ndsu.py
Scrapes North Dakota State Bison baseball stats from two sources:
  1. gobison.com/sports/baseball/roster (Playwright) -> jersey #, position, academic year
  2. stats.gobison.com/custompages/bsb/2026/teamcume.htm (urllib) -> cumulative batting & pitching stats

Merges roster metadata into stats by matching player names and outputs docs/ndsu.json.

Usage:
  python scraper/scrape_ndsu.py
  python scraper/scrape_ndsu.py --out docs/ndsu.json --year 2026
"""

import argparse
import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

DEFAULT_STATS_URL  = "https://stats.gobison.com/custompages/bsb/2026/teamcume.htm"
DEFAULT_ROSTER_URL = "https://gobison.com/sports/baseball/roster"
DEFAULT_OUT        = "docs/ndsu.json"
DEFAULT_YEAR       = "2026"

FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Fetch Helpers ──────────────────────────────────────────────────────────────
def fetch_html_static(url):
    """Plain urllib fetch for static HTML pages (StatBroadcast)."""
    req = urllib.request.Request(url, headers=FETCH_HEADERS)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", errors="replace")


def fetch_html_playwright(url):
    """Playwright fetch for JS-rendered roster page."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=FETCH_HEADERS["User-Agent"])
        page = context.new_page()
        print(f"  [Playwright] Navigating to: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            # Wait for Sidearm roster cards or tables to appear
            page.wait_for_selector(
                ".sidearm-roster-player, .sidearm-roster-players, table, script#__NUXT_DATA__",
                timeout=25000
            )
        except Exception:
            page.wait_for_timeout(5000)
        html = page.content()
        browser.close()
        return html


# ── String & Number Cleaners ───────────────────────────────────────────────────
def clean(val, default="0"):
    if val is None:
        return default
    s = str(val).strip().replace("\xa0", "").replace("&nbsp;", "")
    if s in ("", "-", "--", "---"):
        return default
    return s


def clean_avg(val, default=".000"):
    if val is None:
        return default
    s = str(val).strip().replace("\xa0", "").replace("&nbsp;", "")
    if s in ("", "-", "--", "---", "-.---"):
        return default
    return s


def parse_float(val):
    try:
        return float(str(val).strip().replace("\xa0", ""))
    except (ValueError, TypeError):
        return 0.0


def parse_ip(ip_str):
    """StatBroadcast IP format: '42.1' = 42 and 1/3 innings."""
    try:
        parts = str(ip_str).split(".")
        full = int(parts[0])
        outs = int(parts[1]) if len(parts) > 1 else 0
        return full + outs / 3.0
    except Exception:
        return 0.0


def compute_ops(obp_str, slg_str):
    try:
        return f"{parse_float(obp_str) + parse_float(slg_str):.3f}"
    except Exception:
        return ".000"


def compute_whip(bb, h, ip_str):
    try:
        ip_dec = parse_ip(ip_str)
        if ip_dec <= 0:
            return "-"
        return f"{(parse_float(bb) + parse_float(h)) / ip_dec:.2f}"
    except Exception:
        return "-"


# ── Name Normalizer & Matching ─────────────────────────────────────────────────
def get_name_parts(raw_name):
    """
    Given 'Becker, Colten' or 'Colten Becker' or 'Becker, Colten (25)'
    Returns (first_name_lower, last_name_lower).
    """
    clean_n = re.sub(r"\([^)]*\)", "", raw_name)
    clean_n = re.sub(r"^\d+\s+", "", clean_n).strip()
    clean_n = re.sub(r"[^a-zA-Z\s,]", "", clean_n)

    if "," in clean_n:
        parts = clean_n.split(",", 1)
        last = parts[0].strip().lower()
        first = parts[1].strip().lower().split()[0] if parts[1].strip() else ""
        return first, last
    else:
        tokens = clean_n.strip().lower().split()
        if len(tokens) == 1:
            return "", tokens[0]
        elif len(tokens) >= 2:
            return tokens[0], tokens[-1]
    return "", ""


def match_player_roster(stat_name, roster_list):
    """
    Finds the best matching player from the roster list.
    """
    s_first, s_last = get_name_parts(stat_name)
    if not s_last:
        return None

    # 1. Exact match on both first and last
    for r in roster_list:
        r_first, r_last = get_name_parts(r["name"])
        if s_last == r_last and s_first and r_first and (s_first == r_first or s_first.startswith(r_first) or r_first.startswith(s_first)):
            return r

    # 2. Match on last name only
    candidates = [r for r in roster_list if get_name_parts(r["name"])[1] == s_last]
    if len(candidates) == 1:
        return candidates[0]

    return None


# ── Roster Scraper ─────────────────────────────────────────────────────────────
def scrape_roster(roster_url):
    """
    Scrapes gobison.com/sports/baseball/roster.
    Returns a list of dicts: [{'name': 'Colten Becker', 'number': '25', 'position': 'INF', 'year': 'Sr.'}, ...]
    """
    print("  Fetching NDSU roster (Playwright)...")
    html = fetch_html_playwright(roster_url)
    soup = BeautifulSoup(html, "html.parser")

    roster = []

    # Method A: Sidearm Roster Card Elements
    cards = soup.select(".sidearm-roster-player, .sidearm-roster-item, [class*='roster-player-item']")
    if cards:
        for card in cards:
            name_el = card.select_one(".sidearm-roster-player-name, h3, .roster-player-name")
            num_el  = card.select_one(".sidearm-roster-player-jersey-number, .roster-jersey, .sidearm-roster-player-academic-year")
            pos_el  = card.select_one(".sidearm-roster-player-position, .roster-position, .sidearm-roster-player-custom1")
            yr_el   = card.select_one(".sidearm-roster-player-academic-year, .roster-class, .sidearm-roster-player-academic-class")

            name = name_el.get_text(" ", strip=True) if name_el else ""
            num  = re.sub(r"[^\d]", "", num_el.get_text(strip=True)) if num_el else ""
            pos  = pos_el.get_text(" ", strip=True) if pos_el else ""
            yr   = yr_el.get_text(" ", strip=True) if yr_el else ""

            if name and (num or pos):
                roster.append({
                    "name": name,
                    "number": num if num else "999",
                    "position": pos,
                    "year": yr
                })

    # Method B: Table Rows
    if not roster:
        for tr in soup.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if len(cells) >= 4:
                # Common column layouts: #, Name, Pos, Ht, Wt, Yr
                row_str = " ".join(cells).lower()
                if "player" in row_str or "pos" in row_str or "year" in row_str:
                    continue

                num = re.sub(r"[^\d]", "", cells[0])
                name = cells[1]
                pos = cells[2] if len(cells) > 2 else ""
                yr = cells[3] if len(cells) > 3 else ""

                if num and len(name.split()) >= 2:
                    roster.append({
                        "name": name,
                        "number": num,
                        "position": pos,
                        "year": yr
                    })

    # Method C: Nuxt 3 devalue JSON payload
    if not roster:
        tag = soup.find("script", id="__NUXT_DATA__")
        if tag and tag.string:
            try:
                pool = json.loads(tag.string.strip())
                for item in pool:
                    if isinstance(item, dict):
                        name = str(item.get("name", item.get("playerName", ""))).strip()
                        num  = str(item.get("jerseyNumber", item.get("uniform", ""))).strip()
                        pos  = str(item.get("position", item.get("pos", ""))).strip()
                        yr   = str(item.get("academicYear", item.get("eligibility", ""))).strip()
                        if name and num:
                            roster.append({
                                "name": name,
                                "number": num,
                                "position": pos,
                                "year": yr
                            })
            except Exception:
                pass

    print(f"  Roster extracted: {len(roster)} players found.")
    return roster


# ── StatBroadcast Table Parsers ────────────────────────────────────────────────
def extract_table_rows(table):
    all_rows = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if any(c.strip() for c in cells):
            all_rows.append(cells)
    return all_rows


def parse_batting_table(soup):
    batting_players = []

    target_table = None
    for table in soup.find_all("table"):
        text = table.get_text(" ", strip=True).lower()
        if "avg" in text and "ab" in text and ("rbi" in text or "slg%" in text or "gp-gs" in text):
            target_table = table
            break

    if not target_table:
        print("  WARNING: Could not identify Batting table.")
        return batting_players

    rows = extract_table_rows(target_table)
    if len(rows) < 2:
        return batting_players

    header_idx = None
    header_cols = []
    for idx, row in enumerate(rows):
        row_lower = [c.lower() for c in row]
        if "avg" in row_lower and "ab" in row_lower:
            header_idx = idx
            header_cols = row_lower
            break

    if header_idx is None:
        return batting_players

    def get_c(cands):
        for c in cands:
            for i, h in enumerate(header_cols):
                if h == c.lower():
                    return i
        return None

    c_player = get_c(["player", "name", "batter"])
    c_avg    = get_c(["avg"])
    c_ab     = get_c(["ab"])
    c_r      = get_c(["r"])
    c_h      = get_c(["h"])
    c_2b     = get_c(["2b"])
    c_3b     = get_c(["3b"])
    c_hr     = get_c(["hr"])
    c_rbi    = get_c(["rbi"])
    c_slg    = get_c(["slg%", "slg"])
    c_obp    = get_c(["ob%", "obp"])
    c_ops    = get_c(["ops"])

    def cell(row, idx, default="0"):
        if idx is None or idx >= len(row):
            return default
        return row[idx].strip()

    for row in rows[header_idx + 1:]:
        if len(row) <= 2:
            continue

        raw_player = cell(row, c_player, "").strip() if c_player is not None else row[0].strip()
        if not raw_player or raw_player.lower().startswith("total") or raw_player.lower().startswith("opponent"):
            continue

        avg = clean_avg(cell(row, c_avg))
        ab  = clean(cell(row, c_ab))
        r   = clean(cell(row, c_r))
        h   = clean(cell(row, c_h))
        d2  = clean(cell(row, c_2b))
        d3  = clean(cell(row, c_3b))
        hr  = clean(cell(row, c_hr))
        rbi = clean(cell(row, c_rbi))
        slg = clean_avg(cell(row, c_slg))
        obp = clean_avg(cell(row, c_obp))
        ops = clean_avg(cell(row, c_ops)) if c_ops is not None else compute_ops(obp, slg)

        if parse_float(ab) > 0 or parse_float(h) > 0 or parse_float(r) > 0:
            batting_players.append({
                "stat_name": raw_player,
                "AVG":       avg,
                "R":         r,
                "H":         h,
                "2B":        d2,
                "3B":        d3,
                "HR":        hr,
                "RBI":       rbi,
                "OPS":       ops,
                "SLG%":      slg,
            })

    return batting_players


def parse_pitching_table(soup):
    pitching_players = []

    target_table = None
    for table in soup.find_all("table"):
        text = table.get_text(" ", strip=True).lower()
        if "era" in text and "w-l" in text and "ip" in text and ("er" in text or "bb" in text):
            target_table = table
            break

    if not target_table:
        print("  WARNING: Could not identify Pitching table.")
        return pitching_players

    rows = extract_table_rows(target_table)
    if len(rows) < 2:
        return pitching_players

    header_idx = None
    header_cols = []
    for idx, row in enumerate(rows):
        row_lower = [c.lower() for c in row]
        if "era" in row_lower and "ip" in row_lower and "w-l" in row_lower:
            header_idx = idx
            header_cols = row_lower
            break

    if header_idx is None:
        return pitching_players

    def get_c(cands):
        for c in cands:
            for i, h in enumerate(header_cols):
                if h == c.lower():
                    return i
        return None

    c_player = get_c(["player", "name", "pitcher"])
    c_era    = get_c(["era"])
    c_wl     = get_c(["w-l", "w-l-s", "wl"])
    c_ip     = get_c(["ip"])
    c_h      = get_c(["h"])
    c_bb     = get_c(["bb"])
    c_so     = get_c(["so", "k"])
    c_whip   = get_c(["whip"])

    def cell(row, idx, default="0"):
        if idx is None or idx >= len(row):
            return default
        return row[idx].strip()

    for row in rows[header_idx + 1:]:
        if len(row) <= 2:
            continue

        raw_player = cell(row, c_player, "").strip() if c_player is not None else row[0].strip()
        if not raw_player or raw_player.lower().startswith("total") or raw_player.lower().startswith("opponent"):
            continue

        ip_raw = cell(row, c_ip, "0.0")
        ip_dec = parse_ip(ip_raw)
        if ip_dec == 0:
            continue

        era  = clean_avg(cell(row, c_era))
        wl   = clean(cell(row, c_wl, "0-0"), "0-0")
        h    = clean(cell(row, c_h))
        bb   = clean(cell(row, c_bb))
        so   = clean(cell(row, c_so))
        whip = clean_avg(cell(row, c_whip)) if c_whip is not None else compute_whip(bb, h, ip_raw)

        pitching_players.append({
            "stat_name": raw_player,
            "ERA":       era,
            "WHIP":      whip,
            "W-L":       wl,
            "IP":        ip_raw,
            "SO":        so,
        })

    return pitching_players


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="NDSU Baseball Scraper (Stats + Roster)")
    parser.add_argument("--out",        default=DEFAULT_OUT,        help="Output JSON path")
    parser.add_argument("--year",       default=DEFAULT_YEAR,       help="Season year")
    parser.add_argument("--stats-url",  default=DEFAULT_STATS_URL,  help="StatBroadcast teamcume.htm URL")
    parser.add_argument("--roster-url", default=DEFAULT_ROSTER_URL, help="gobison.com roster URL")
    args = parser.parse_args()

    stats_url = args.stats_url.replace("2026", args.year) if args.year != "2026" else args.stats_url

    # 1. Fetch Roster
    print("Step 1: Scraping NDSU roster from gobison.com...")
    roster_list = scrape_roster(args.roster_url)

    # 2. Fetch StatBroadcast HTML
    print(f"\nStep 2: Fetching NDSU stats page: {stats_url}")
    try:
        html = fetch_html_static(stats_url)
    except Exception as e:
        print(f"  Fetch failed: {e}")
        return

    soup = BeautifulSoup(html, "html.parser")

    # 3. Parse Batting and Pitching
    print("\nStep 3: Parsing batting table...")
    raw_batting = parse_batting_table(soup)
    print(f"  Found {len(raw_batting)} batters.")

    print("\nStep 4: Parsing pitching table...")
    raw_pitching = parse_pitching_table(soup)
    print(f"  Found {len(raw_pitching)} pitchers.")

    # 4. Merge Roster Metadata & Prevent 999 Overwriting
    fallback_id_counter = 1000

    hitting_list = []
    pitching_list = []
    hitting_by_num = {}
    pitching_by_num = {}
    players_map = {}

    for b in raw_batting:
        match = match_player_roster(b["stat_name"], roster_list)
        if match and match["number"] != "999":
            num = match["number"]
            name = match["name"]
            pos = match["position"]
            yr = match["year"]
            jersey_int = int(num) if num.isdigit() else fallback_id_counter
        else:
            fallback_id_counter += 1
            jersey_int = fallback_id_counter
            num = str(jersey_int)
            name = b["stat_name"]
            pos = ""
            yr = ""

        bat_item = {
            "number":   num,
            "player":   name,
            "position": pos,
            "year":     yr,
            "AVG":      b["AVG"],
            "R":        b["R"],
            "H":        b["H"],
            "2B":       b["2B"],
            "3B":       b["3B"],
            "HR":       b["HR"],
            "RBI":      b["RBI"],
            "OPS":      b["OPS"],
            "SLG%":     b["SLG%"],
            "_sort":    jersey_int
        }
        hitting_list.append(bat_item)
        hitting_by_num[num] = {k: v for k, v in bat_item.items() if k != "_sort"}

        p = players_map.get(jersey_int, {
            "jersey_number": jersey_int,
            "player_name":   name,
            "position":      pos,
            "year":          yr
        })
        p["batting_stats"] = {
            "AVG": b["AVG"], "R": b["R"], "H": b["H"], "2B": b["2B"], "3B": b["3B"],
            "HR": b["HR"], "RBI": b["RBI"], "OPS": b["OPS"], "SLG%": b["SLG%"]
        }
        players_map[jersey_int] = p

    for p_stat in raw_pitching:
        match = match_player_roster(p_stat["stat_name"], roster_list)
        if match and match["number"] != "999":
            num = match["number"]
            name = match["name"]
            pos = match["position"]
            yr = match["year"]
            jersey_int = int(num) if num.isdigit() else fallback_id_counter
        else:
            fallback_id_counter += 1
            jersey_int = fallback_id_counter
            num = str(jersey_int)
            name = p_stat["stat_name"]
            pos = ""
            yr = ""

        pit_item = {
            "number":   num,
            "player":   name,
            "position": pos,
            "year":     yr,
            "ERA":      p_stat["ERA"],
            "WHIP":     p_stat["WHIP"],
            "W-L":      p_stat["W-L"],
            "IP":       p_stat["IP"],
            "SO":       p_stat["SO"],
            "_sort":    jersey_int
        }
        pitching_list.append(pit_item)
        pitching_by_num[num] = {k: v for k, v in pit_item.items() if k != "_sort"}

        p = players_map.get(jersey_int, {
            "jersey_number": jersey_int,
            "player_name":   name,
            "position":      pos,
            "year":          yr
        })
        p["pitching_stats"] = {
            "ERA": p_stat["ERA"], "WHIP": p_stat["WHIP"], "W-L": p_stat["W-L"],
            "IP": p_stat["IP"], "SO": p_stat["SO"]
        }
        players_map[jersey_int] = p

    hitting_list.sort(key=lambda x: x.pop("_sort"))
    pitching_list.sort(key=lambda x: x.pop("_sort"))
    players_sorted = [players_map[k] for k in sorted(players_map.keys())]

    result = {
        "team_id":            "ndsu",
        "team":               "North Dakota State Bison",
        "sport":              "baseball",
        "season":             args.year,
        "source_stats":       stats_url,
        "source_roster":      args.roster_url,
        "status":             "ok",
        "generated_at":       datetime.now(timezone.utc).isoformat(),
        "players":            players_sorted,
        "hitting":            hitting_list,
        "pitching":           pitching_list,
        "hitting_by_number":  hitting_by_num,
        "pitching_by_number": pitching_by_num,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(f"\nDone — {len(hitting_list)} batters, {len(pitching_list)} pitchers -> {args.out}")


if __name__ == "__main__":
    main()
