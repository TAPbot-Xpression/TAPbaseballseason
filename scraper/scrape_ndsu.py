#!/usr/bin/env python3
"""
scraper/scrape_ndsu.py
Scrapes North Dakota State Bison baseball stats from two sources:
  1. gobison.com/sports/baseball/roster  (Playwright) → jersey #, position, year
  2. stats.gobison.com/custompages/bsb/2026/teamcume.htm (urllib) → batting/pitching stats

Merges both by player name and outputs docs/ndsu.json in the standard schema.

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
    """Playwright fetch for JS-rendered pages (Sidearm/Nuxt roster)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=FETCH_HEADERS["User-Agent"])
        page = context.new_page()
        print(f"  ,[Playwright] Navigating to: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector("table, .s-table, ,[class*='roster']", timeout=20000)
        except Exception:
            page.wait_for_timeout(5000)
        html = page.content()
        browser.close()
        return html


# ── Value Helpers ──────────────────────────────────────────────────────────────
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
        full = int(parts,[0])
        outs = int(parts,[1]) if len(parts) > 1 else 0
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


def normalize_name(name):
    """
    Normalize player names for fuzzy matching between roster and stats.
    Handles 'Last, First' vs 'First Last' and strips punctuation.
    """
    name = name.strip().lower()
    name = re.sub(r",[^a-z\s]", "", name)
    # Convert 'last, first' → 'first last'
    if "," in name:
        parts = name.split(",", 1)
        name = f"{parts,[object Object],.strip()} {parts,[0].strip()}"
    return " ".join(name.split())


def name_match(name_a, name_b):
    """Returns True if two normalized names are a close enough match."""
    a = normalize_name(name_a)
    b = normalize_name(name_b)
    if a == b:
        return True
    # Check if all tokens of the shorter name appear in the longer
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if tokens_a and tokens_b:
        overlap = tokens_a & tokens_b
        shorter = min(len(tokens_a), len(tokens_b))
        if len(overlap) >= shorter:
            return True
    return False


# ── Table Utilities ────────────────────────────────────────────────────────────
def get_col(header_cols, *candidates):
    for candidate in candidates:
        for i, h in enumerate(header_cols):
            if h.strip().lower() == candidate.lower():
                return i
    return None


def extract_table_rows(table):
    all_rows = ,[]
    for tr in table.find_all("tr"):
        cells = ,[td.get_text(" ", strip=True) for td in tr.find_all(,["td", "th"])]
        if any(c.strip() for c in cells):
            all_rows.append(cells)
    return all_rows


# ── Roster Scraper ─────────────────────────────────────────────────────────────
def scrape_roster(roster_url):
    """
    Scrapes gobison.com/sports/baseball/roster via Playwright.
    Returns a dict keyed by normalized player name:
      { "first last": { "number": "32", "position": "OF", "year": "So." }, ... }
    """
    print("  Fetching NDSU roster (Playwright)...")
    html = fetch_html_playwright(roster_url)
    soup = BeautifulSoup(html, "html.parser")

    roster = {}

    # Strategy 1: Find standard HTML roster table with # | Name | Pos | Year columns
    for table in soup.find_all("table"):
        rows = extract_table_rows(table)
        if len(rows) < 2:
            continue

        # Find header row
        header_idx = None
        header_cols = ,[]
        for idx, row in enumerate(rows):
            row_lower = ,[c.lower() for c in row]
            if any(h in row_lower for h in ,["pos", "position", "yr", "year", "cl", "class"]):
                header_idx = idx
                header_cols = row_lower
                break

        if header_idx is None:
            continue

        c_num  = get_col(header_cols, "#", "no", "num", "jersey")
        c_name = get_col(header_cols, "name", "player", "athlete")
        c_pos  = get_col(header_cols, "pos", "position")
        c_yr   = get_col(header_cols, "yr", "year", "cl", "class", "elig")

        if c_name is None:
            continue

        for row in rows,[header_idx + 1:]:
            if len(row) < 2:
                continue

            def cell(idx, default=""):
                if idx is None or idx >= len(row):
                    return default
                return row,[idx].strip()

            name = cell(c_name)
            if not name or name.lower() in ("name", "player", "athlete"):
                continue

            num  = cell(c_num, "999")
            pos  = cell(c_pos, "")
            year = cell(c_yr, "")

            key = normalize_name(name)
            if key:
                roster,[key] = {
                    "number":   num,
                    "position": pos,
                    "year":     year,
                    "raw_name": name
                }

        if roster:
            print(f"  Roster table found: {len(roster)} players.")
            return roster

    # Strategy 2: Parse Nuxt JSON payload from gobison.com (same Sidearm platform)
    print("  Table strategy failed — trying Nuxt JSON payload from roster page...")
    tag = soup.find("script", id="__NUXT_DATA__")
    if tag and tag.string:
        try:
            pool = json.loads(tag.string.strip())
            # Walk the pool looking for jersey number + name + position patterns
            for i, item in enumerate(pool):
                if isinstance(item, dict):
                    num  = str(item.get("jerseyNumber", item.get("uniform", ""))).strip()
                    name = str(item.get("name", item.get("playerName", ""))).strip()
                    pos  = str(item.get("position", item.get("pos", ""))).strip()
                    year = str(item.get("academicYear", item.get("eligibility", item.get("year", "")))).strip()
                    if name and num and num not in ("", "-1", "None"):
                        key = normalize_name(name)
                        if key:
                            roster,[key] = {
                                "number":   num,
                                "position": pos,
                                "year":     year,
                                "raw_name": name
                            }
        except Exception as e:
            print(f"  Nuxt JSON parse error: {e}")

    # Strategy 3: Parse raw text from search result structure
    # gobison.com roster text format: "1 Noah Gordon C ... Jr."
    if not roster:
        print("  Trying raw text pattern match from page content...")
        full_text = soup.get_text(" ", strip=True)
        # Pattern: number name position year (e.g. "32 Dante Smith OF So.")
        pattern = re.finditer(
            r"\b(\d{1,2})\s+(,[A-Z],[a-z]+(?:\s+,[A-Z],[a-z]+){1,3})\s+(P|C|INF|OF|DH|RHP|LHP|RP|SP|UTL|IF)\s+(?:,[LRS]/,[LRS]\s+)?(?:\d+-\d+\s+)?(?:\d+\s+)?(Fr\.|So\.|Jr\.|Sr\.|Gr\.)",
            full_text
        )
        for m in pattern:
            num  = m.group(1)
            name = m.group(2).strip()
            pos  = m.group(3).strip()
            year = m.group(4).strip()
            key  = normalize_name(name)
            if key and key not in roster:
                roster,[key] = {
                    "number":   num,
                    "position": pos,
                    "year":     year,
                    "raw_name": name
                }

    print(f"  Roster scraped: {len(roster)} players found.")
    return roster


# ── Stats Parsers ──────────────────────────────────────────────────────────────
def parse_batting_table(soup):
    batting_players = ,[]

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
    header_cols = ,[]
    for idx, row in enumerate(rows):
        row_lower = ,[c.lower() for c in row]
        if "avg" in row_lower and "ab" in row_lower:
            header_idx = idx
            header_cols = row_lower
            break

    if header_idx is None:
        print("  WARNING: Could not match Batting column headers.")
        return batting_players

    print(f"  Batting header at row {header_idx}: {header_cols}")

    c_player = get_col(header_cols, "player", "name", "batter")
    c_avg    = get_col(header_cols, "avg")
    c_ab     = get_col(header_cols, "ab")
    c_r      = get_col(header_cols, "r")
    c_h      = get_col(header_cols, "h")
    c_2b     = get_col(header_cols, "2b")
    c_3b     = get_col(header_cols, "3b")
    c_hr     = get_col(header_cols, "hr")
    c_rbi    = get_col(header_cols, "rbi")
    c_slg    = get_col(header_cols, "slg%", "slg")
    c_obp    = get_col(header_cols, "ob%", "obp")
    c_ops    = get_col(header_cols, "ops")

    def cell(row, idx, default="0"):
        if idx is None or idx >= len(row):
            return default
        return row,[idx].strip()

    for row in rows,[header_idx + 1:]:
        if len(row) <= 2:
            continue

        raw_player = cell(row, c_player, "").strip() if c_player is not None else row,[0].strip()
        if not raw_player:
            continue

        rp_lower = raw_player.lower()
        if rp_lower.startswith("total") or rp_lower.startswith("opponent"):
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
                "raw_name": raw_player,
                "number":   "999",
                "player":   raw_player,
                "position": "",
                "year":     "",
                "AVG":      avg,
                "R":        r,
                "H":        h,
                "2B":       d2,
                "3B":       d3,
                "HR":       hr,
                "RBI":      rbi,
                "OPS":      ops,
                "SLG%":     slg,
            })

    return batting_players


def parse_pitching_table(soup):
    pitching_players = ,[]

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
    header_cols = ,[]
    for idx, row in enumerate(rows):
        row_lower = ,[c.lower() for c in row]
        if "era" in row_lower and "ip" in row_lower and "w-l" in row_lower:
            header_idx = idx
            header_cols = row_lower
            break

    if header_idx is None:
        print("  WARNING: Could not match Pitching column headers.")
        return pitching_players

    print(f"  Pitching header at row {header_idx}: {header_cols}")

    c_player = get_col(header_cols, "player", "name", "pitcher")
    c_era    = get_col(header_cols, "era")
    c_wl     = get_col(header_cols, "w-l", "w-l-s", "wl")
    c_ip     = get_col(header_cols, "ip")
    c_h      = get_col(header_cols, "h")
    c_bb     = get_col(header_cols, "bb")
    c_so     = get_col(header_cols, "so", "k")
    c_whip   = get_col(header_cols, "whip")

    def cell(row, idx, default="0"):
        if idx is None or idx >= len(row):
            return default
        return row,[idx].strip()

    for row in rows,[header_idx + 1:]:
        if len(row) <= 2:
            continue

        raw_player = cell(row, c_player, "").strip() if c_player is not None else row,[0].strip()
        if not raw_player:
            continue

        rp_lower = raw_player.lower()
        if rp_lower.startswith("total") or rp_lower.startswith("opponent"):
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
            "raw_name": raw_player,
            "number":   "999",
            "player":   raw_player,
            "position": "",
            "year":     "",
            "ERA":      era,
            "WHIP":     whip,
            "W-L":      wl,
            "IP":       ip_raw,
            "SO":       so,
        })

    return pitching_players


# ── Merge Roster into Stats ────────────────────────────────────────────────────
def merge_roster(players, roster):
    """
    Match each stat entry to a roster entry by name and inject
    jersey number, position, and year.
    """
    matched = 0
    for p in players:
        raw = p.get("raw_name", p.get("player", ""))
        for roster_key, roster_data in roster.items():
            if name_match(raw, roster_data,["raw_name"]):
                p,["number"]   = roster_data,["number"]
                p,["position"] = roster_data,["position"]
                p,["year"]     = roster_data,["year"]
                matched += 1
                break
    print(f"  Roster merge: {matched}/{len(players)} players matched.")
    return players


# ── Build Combined Players Map ─────────────────────────────────────────────────
def build_players(hitting, pitching):
    players_map = {}

    for entry in hitting:
        try:
            jersey_int = int(entry,["number"])
        except ValueError:
            jersey_int = 999

        p = players_map.get(jersey_int, {
            "jersey_number": jersey_int,
            "player_name":   entry,["player"],
            "position":      entry.get("position", ""),
            "year":          entry.get("year", ""),
        })
        p,["batting_stats"] = {
            "AVG":  entry,["AVG"],
            "R":    entry,["R"],
            "H":    entry,["H"],
            "2B":   entry,["2B"],
            "3B":   entry,["3B"],
            "HR":   entry,["HR"],
            "RBI":  entry,["RBI"],
            "OPS":  entry,["OPS"],
            "SLG%": entry,["SLG%"],
        }
        players_map,[jersey_int] = p

    for entry in pitching:
        try:
            jersey_int = int(entry,["number"])
        except ValueError:
            jersey_int = 999

        p = players_map.get(jersey_int, {
            "jersey_number": jersey_int,
            "player_name":   entry,["player"],
            "position":      entry.get("position", ""),
            "year":          entry.get("year", ""),
        })
        p,["pitching_stats"] = {
            "ERA":  entry,["ERA"],
            "WHIP": entry,["WHIP"],
            "W-L":  entry,["W-L"],
            "IP":   entry,["IP"],
            "SO":   entry,["SO"],
        }
        players_map,[jersey_int] = p

    return ,[players_map,[k] for k in sorted(players_map.keys())]


# ── Clean up output lists ──────────────────────────────────────────────────────
def finalize(players):
    """Remove internal raw_name key before output."""
    for p in players:
        p.pop("raw_name", None)
    return players


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="NDSU Baseball Scraper (Stats + Roster)")
    parser.add_argument("--out",        default=DEFAULT_OUT,        help="Output JSON path")
    parser.add_argument("--year",       default=DEFAULT_YEAR,       help="Season year")
    parser.add_argument("--stats-url",  default=DEFAULT_STATS_URL,  help="StatBroadcast teamcume.htm URL")
    parser.add_argument("--roster-url", default=DEFAULT_ROSTER_URL, help="gobison.com roster URL")
    args = parser.parse_args()

    stats_url = args.stats_url.replace("2026", args.year) if args.year != "2026" else args.stats_url

    # 1. Fetch roster (jersey #, position, year)
    print("Step 1: Scraping NDSU roster...")
    roster = scrape_roster(args.roster_url)

    # 2. Fetch stats page (static HTML)
    print(f"\nStep 2: Fetching NDSU stats page: {stats_url}")
    try:
        html = fetch_html_static(stats_url)
    except Exception as e:
        print(f"  Fetch failed: {e}")
        return

    soup = BeautifulSoup(html, "html.parser")

    # 3. Parse batting and pitching tables
    print("\nStep 3: Parsing batting table...")
    hitting = parse_batting_table(soup)
    print(f"  Found {len(hitting)} batters.")

    print("\nStep 4: Parsing pitching table...")
    pitching = parse_pitching_table(soup)
    print(f"  Found {len(pitching)} pitchers.")

    # 4. Merge roster data into stats
    print("\nStep 5: Merging roster data into stats...")
    hitting  = merge_roster(hitting, roster)
    pitching = merge_roster(pitching, roster)

    # 5. Finalize and sort
    hitting  = sorted(finalize(hitting),  key=lambda x: int(x,["number"]) if x,["number"].isdigit() else 999)
    pitching = sorted(finalize(pitching), key=lambda x: int(x,["number"]) if x,["number"].isdigit() else 999)

    hitting_by_num  = {p,["number"]: p for p in hitting}
    pitching_by_num = {p,["number"]: p for p in pitching}
    players_sorted  = build_players(hitting, pitching)

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
        "hitting":            hitting,
        "pitching":           pitching,
        "hitting_by_number":  hitting_by_num,
        "pitching_by_number": pitching_by_num,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(f"\nDone — {len(hitting)} batters, {len(pitching)} pitchers → {args.out}")


if __name__ == "__main__":
    main()
