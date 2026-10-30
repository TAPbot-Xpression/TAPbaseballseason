#!/usr/bin/env python3
"""
scraper/scrape_ndsu.py
Scrapes North Dakota State Bison baseball stats from StatBroadcast HTML (teamcume.htm).
Locates tables by matching column header signatures:
  - Batting:  table containing "avg", "ab", "rbi", "slg%"
  - Pitching: table containing "era", "w-l", "ip", "so"
Outputs to docs/ndsu.json in the standard schema.

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

DEFAULT_URL  = "https://stats.gobison.com/custompages/bsb/2026/teamcume.htm"
DEFAULT_OUT  = "docs/ndsu.json"
DEFAULT_YEAR = "2026"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Fetch ──────────────────────────────────────────────────────────────────────
def fetch_html(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", errors="replace")


# ── Helpers ────────────────────────────────────────────────────────────────────
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
    """Parses StatBroadcast IP format: '42.1' = 42 and 1/3 innings."""
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


def split_jersey_and_name(text):
    """
    Extracts jersey number and name from StatBroadcast player cell text.
    Handles formats like:
      '25 Becker, Colten'
      '3 Smith, Dante'
      'Becker, Colten' (no number)
    """
    text = text.strip()
    # Pattern: leading 1-2 digit number followed by space then name
    m = re.match(r"^(\d{1,2})\s+(.+)$", text)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    # Pattern: name followed by trailing number
    m2 = re.search(r"^(.*?)\s+(\d{1,2})$", text)
    if m2:
        return m2.group(2).strip(), m2.group(1).strip()
    return "999", text


# ── Table Utilities ────────────────────────────────────────────────────────────
def get_col(header_cols, *candidates):
    """Return index of first matching candidate in header_cols list."""
    for candidate in candidates:
        for i, h in enumerate(header_cols):
            if h.strip().lower() == candidate.lower():
                return i
    return None


def extract_table_rows(table):
    """Extract all non-empty rows from a BeautifulSoup table as lists of cell text."""
    all_rows = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if any(c.strip() for c in cells):
            all_rows.append(cells)
    return all_rows


# ── Batting Parser ─────────────────────────────────────────────────────────────
def parse_batting_table(soup):
    batting_players = []

    # Find table whose text contains batting header signature
    target_table = None
    for table in soup.find_all("table"):
        text = table.get_text(" ", strip=True).lower()
        if "avg" in text and "ab" in text and ("rbi" in text or "slg%" in text or "gp-gs" in text):
            target_table = table
            break

    if not target_table:
        print("  WARNING: Could not identify Batting table by header signature.")
        return batting_players

    rows = extract_table_rows(target_table)
    if len(rows) < 2:
        return batting_players

    # Find header row containing 'avg' and 'ab'
    header_idx = None
    header_cols = []
    for idx, row in enumerate(rows):
        row_lower = [c.lower() for c in row]
        if "avg" in row_lower and "ab" in row_lower:
            header_idx = idx
            header_cols = row_lower
            break

    if header_idx is None:
        print("  WARNING: Could not match Batting column headers.")
        return batting_players

    print(f"  Batting header found at row {header_idx}: {header_cols}")

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
        return row[idx].strip()

    for row in rows[header_idx + 1:]:
        if len(row) <= 2:
            continue

        raw_player = cell(row, c_player, "").strip() if c_player is not None else row[0].strip()
        if not raw_player:
            continue

        # Skip Totals and Opponents rows
        rp_lower = raw_player.lower()
        if rp_lower.startswith("total") or rp_lower.startswith("opponent"):
            continue

        num, name = split_jersey_and_name(raw_player)

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
                "number": num,
                "player": name,
                "AVG":    avg,
                "R":      r,
                "H":      h,
                "2B":     d2,
                "3B":     d3,
                "HR":     hr,
                "RBI":    rbi,
                "OPS":    ops,
                "SLG%":   slg,
            })

    return batting_players


# ── Pitching Parser ────────────────────────────────────────────────────────────
def parse_pitching_table(soup):
    pitching_players = []

    # Find table whose text contains pitching header signature
    target_table = None
    for table in soup.find_all("table"):
        text = table.get_text(" ", strip=True).lower()
        if "era" in text and "w-l" in text and "ip" in text and ("er" in text or "bb" in text):
            target_table = table
            break

    if not target_table:
        print("  WARNING: Could not identify Pitching table by header signature.")
        return pitching_players

    rows = extract_table_rows(target_table)
    if len(rows) < 2:
        return pitching_players

    # Find header row containing 'era', 'ip', 'w-l'
    header_idx = None
    header_cols = []
    for idx, row in enumerate(rows):
        row_lower = [c.lower() for c in row]
        if "era" in row_lower and "ip" in row_lower and "w-l" in row_lower:
            header_idx = idx
            header_cols = row_lower
            break

    if header_idx is None:
        print("  WARNING: Could not match Pitching column headers.")
        return pitching_players

    print(f"  Pitching header found at row {header_idx}: {header_cols}")

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
        return row[idx].strip()

    for row in rows[header_idx + 1:]:
        if len(row) <= 2:
            continue

        raw_player = cell(row, c_player, "").strip() if c_player is not None else row[0].strip()
        if not raw_player:
            continue

        # Skip Totals and Opponents rows
        rp_lower = raw_player.lower()
        if rp_lower.startswith("total") or rp_lower.startswith("opponent"):
            continue

        num, name = split_jersey_and_name(raw_player)

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
            "number": num,
            "player": name,
            "ERA":    era,
            "WHIP":   whip,
            "W-L":    wl,
            "IP":     ip_raw,
            "SO":     so,
        })

    return pitching_players


# ── Build Combined Players Map ─────────────────────────────────────────────────
def build_players(hitting, pitching):
    players_map = {}

    for entry in hitting:
        try:
            jersey_int = int(entry["number"])
        except ValueError:
            jersey_int = 999
        p = players_map.get(jersey_int, {
            "jersey_number": jersey_int,
            "player_name":   entry["player"]
        })
        p["batting_stats"] = {
            "AVG":  entry["AVG"],
            "R":    entry["R"],
            "H":    entry["H"],
            "2B":   entry["2B"],
            "3B":   entry["3B"],
            "HR":   entry["HR"],
            "RBI":  entry["RBI"],
            "OPS":  entry["OPS"],
            "SLG%": entry["SLG%"],
        }
        players_map[jersey_int] = p

    for entry in pitching:
        try:
            jersey_int = int(entry["number"])
        except ValueError:
            jersey_int = 999
        p = players_map.get(jersey_int, {
            "jersey_number": jersey_int,
            "player_name":   entry["player"]
        })
        p["pitching_stats"] = {
            "ERA":  entry["ERA"],
            "WHIP": entry["WHIP"],
            "W-L":  entry["W-L"],
            "IP":   entry["IP"],
            "SO":   entry["SO"],
        }
        players_map[jersey_int] = p

    return [players_map[k] for k in sorted(players_map.keys())]


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="NDSU StatBroadcast Baseball Scraper")
    parser.add_argument("--out",  default=DEFAULT_OUT,  help="Output JSON path")
    parser.add_argument("--year", default=DEFAULT_YEAR, help="Season year")
    parser.add_argument("--url",  default=DEFAULT_URL,  help="StatBroadcast teamcume.htm URL")
    args = parser.parse_args()

    url = args.url.replace("2026", args.year) if args.year != "2026" else args.url

    print(f"Fetching NDSU StatBroadcast page: {url}")
    try:
        html = fetch_html(url)
    except Exception as e:
        print(f"  Fetch failed: {e}")
        return

    soup = BeautifulSoup(html, "html.parser")

    print("Parsing batting table...")
    hitting = parse_batting_table(soup)
    print(f"  Found {len(hitting)} batters.")

    print("Parsing pitching table...")
    pitching = parse_pitching_table(soup)
    print(f"  Found {len(pitching)} pitchers.")

    hitting_by_num  = {p["number"]: p for p in hitting}
    pitching_by_num = {p["number"]: p for p in pitching}
    players_sorted  = build_players(hitting, pitching)

    result = {
        "team_id":            "ndsu",
        "team":               "North Dakota State Bison",
        "sport":              "baseball",
        "season":             args.year,
        "source":             url,
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

    print(f"Done — {len(hitting)} batters, {len(pitching)} pitchers → {args.out}")


if __name__ == "__main__":
    main()
