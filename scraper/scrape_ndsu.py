#!/usr/bin/env python3
"""
scraper/scrape_ndsu.py
Scrapes North Dakota State Bison baseball stats from StatBroadcast static HTML.
Parses the teamcume.htm page for individual batting and pitching cumulative stats.
Outputs to docs/ndsu.json in the same schema as the Sidearm scraper.

Usage:
  python scraper/scrape_ndsu.py
  python scraper/scrape_ndsu.py --out docs/ndsu.json --year 2026
"""

import argparse
import json
import os
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
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="replace")


# ── Helpers ────────────────────────────────────────────────────────────────────
def clean(val, default="0"):
    if val is None:
        return default
    s = str(val).strip().replace("\xa0", "")
    if s in ("", "-", "--"):
        return default
    return s


def clean_avg(val, default=".000"):
    if val is None:
        return default
    s = str(val).strip().replace("\xa0", "")
    if s in ("", "-", "--", "-.---"):
        return default
    return s


def parse_float(val):
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return 0.0


def parse_ip(ip_str):
    """
    StatBroadcast stores IP as innings.outs (e.g. 42.1 = 42 and 1/3 innings).
    Returns decimal value for ERA/WHIP math.
    """
    try:
        parts = str(ip_str).split(".")
        full = int(parts[0])
        outs = int(parts[1]) if len(parts) > 1 else 0
        return full + outs / 3
    except Exception:
        return 0.0


def compute_slg(h, d2, d3, hr, ab):
    try:
        ab_f = parse_float(ab)
        if ab_f == 0:
            return ".000"
        tb = parse_float(h) + parse_float(d2) + 2 * parse_float(d3) + 3 * parse_float(hr)
        return f"{tb / ab_f:.3f}"
    except Exception:
        return ".000"


def compute_obp(h, bb, hbp, ab, sf):
    try:
        num = parse_float(h) + parse_float(bb) + parse_float(hbp)
        den = parse_float(ab) + parse_float(bb) + parse_float(hbp) + parse_float(sf)
        if den == 0:
            return ".000"
        return f"{num / den:.3f}"
    except Exception:
        return ".000"


def compute_ops(obp_str, slg_str):
    try:
        return f"{parse_float(obp_str) + parse_float(slg_str):.3f}"
    except Exception:
        return ".000"


def compute_era(er, ip_dec):
    try:
        if ip_dec == 0:
            return "-"
        return f"{(parse_float(er) * 9) / ip_dec:.2f}"
    except Exception:
        return "-"


def compute_whip(bb, h, ip_dec):
    try:
        if ip_dec == 0:
            return "-"
        return f"{(parse_float(bb) + parse_float(h)) / ip_dec:.2f}"
    except Exception:
        return "-"


# ── Table Utilities ────────────────────────────────────────────────────────────
def get_col_index(headers, *candidates):
    for candidate in candidates:
        for i, h in enumerate(headers):
            if h.strip().lower() == candidate.lower():
                return i
    return None


def parse_rows(table):
    rows = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if any(c.strip() for c in cells):
            rows.append(cells)
    return rows


def find_stat_table(soup, keywords):
    """
    Find the table whose text contains any of the given keywords.
    StatBroadcast wraps batting and pitching in separate tables.
    """
    for table in soup.find_all("table"):
        text = table.get_text(" ", strip=True).lower()
        if any(kw.lower() in text for kw in keywords):
            return table
    return None


# ── Batting Parser ─────────────────────────────────────────────────────────────
def parse_batting(soup):
    batting_players = []

    table = find_stat_table(soup, ["batting", "individual batting", "hitting"])
    if not table:
        print("  WARNING: Could not find batting table.")
        return batting_players

    rows = parse_rows(table)
    if len(rows) < 2:
        return batting_players

    # Find header row
    header_row = None
    header_idx = 0
    for i, row in enumerate(rows):
        row_lower = [c.lower() for c in row]
        if "avg" in row_lower or "ab" in row_lower:
            header_row = row_lower
            header_idx = i
            break

    if header_row is None:
        print("  WARNING: Could not identify batting header row.")
        return batting_players

    col = {
        "num":    get_col_index(header_row, "##", "#", "no", "num"),
        "player": get_col_index(header_row, "player", "name"),
        "ab":     get_col_index(header_row, "ab"),
        "r":      get_col_index(header_row, "r"),
        "h":      get_col_index(header_row, "h"),
        "rbi":    get_col_index(header_row, "rbi"),
        "2b":     get_col_index(header_row, "2b"),
        "3b":     get_col_index(header_row, "3b"),
        "hr":     get_col_index(header_row, "hr"),
        "bb":     get_col_index(header_row, "bb"),
        "hbp":    get_col_index(header_row, "hbp"),
        "sf":     get_col_index(header_row, "sf"),
        "avg":    get_col_index(header_row, "avg"),
        "obp":    get_col_index(header_row, "obp"),
        "slg":    get_col_index(header_row, "slg", "slg%"),
        "ops":    get_col_index(header_row, "ops"),
    }

    def cell(row, key, default="0"):
        idx = col.get(key)
        if idx is None or idx >= len(row):
            return default
        return row[idx].strip()

    for row in rows[header_idx + 1:]:
        if not row or len(row) < 3:
            continue

        player_name = cell(row, "player", "").strip()
        if not player_name or player_name.lower() in ("totals", "total", "opponents", "opp"):
            continue

        ab = clean(cell(row, "ab"))
        h  = clean(cell(row, "h"))
        if parse_float(ab) == 0 and parse_float(h) == 0:
            continue

        num = clean(cell(row, "num"), "0")
        r   = clean(cell(row, "r"))
        d2  = clean(cell(row, "2b"))
        d3  = clean(cell(row, "3b"))
        hr  = clean(cell(row, "hr"))
        rbi = clean(cell(row, "rbi"))
        bb  = clean(cell(row, "bb"))
        hbp = clean(cell(row, "hbp"))
        sf  = clean(cell(row, "sf"))
        avg = clean_avg(cell(row, "avg"))

        slg = clean_avg(cell(row, "slg")) if col.get("slg") else compute_slg(h, d2, d3, hr, ab)
        obp = clean_avg(cell(row, "obp")) if col.get("obp") else compute_obp(h, bb, hbp, ab, sf)
        ops = clean_avg(cell(row, "ops")) if col.get("ops") else compute_ops(obp, slg)

        batting_players.append({
            "number": num,
            "player": player_name,
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

    print(f"  Parsed {len(batting_players)} batting entries.")
    return batting_players


# ── Pitching Parser ────────────────────────────────────────────────────────────
def parse_pitching(soup):
    pitching_players = []

    table = find_stat_table(soup, ["pitching", "individual pitching"])
    if not table:
        print("  WARNING: Could not find pitching table.")
        return pitching_players

    rows = parse_rows(table)
    if len(rows) < 2:
        return pitching_players

    # Find header row
    header_row = None
    header_idx = 0
    for i, row in enumerate(rows):
        row_lower = [c.lower() for c in row]
        if "era" in row_lower or "ip" in row_lower:
            header_row = row_lower
            header_idx = i
            break

    if header_row is None:
        print("  WARNING: Could not identify pitching header row.")
        return pitching_players

    col = {
        "num":    get_col_index(header_row, "##", "#", "no", "num"),
        "player": get_col_index(header_row, "player", "name"),
        "w":      get_col_index(header_row, "w"),
        "l":      get_col_index(header_row, "l"),
        "era":    get_col_index(header_row, "era"),
        "ip":     get_col_index(header_row, "ip"),
        "h":      get_col_index(header_row, "h"),
        "er":     get_col_index(header_row, "er"),
        "bb":     get_col_index(header_row, "bb"),
        "so":     get_col_index(header_row, "so", "k"),
        "whip":   get_col_index(header_row, "whip"),
    }

    def cell(row, key, default="0"):
        idx = col.get(key)
        if idx is None or idx >= len(row):
            return default
        return row[idx].strip()

    for row in rows[header_idx + 1:]:
        if not row or len(row) < 3:
            continue

        player_name = cell(row, "player", "").strip()
        if not player_name or player_name.lower() in ("totals", "total", "opponents", "opp"):
            continue

        ip_raw = cell(row, "ip", "0.0")
        ip_dec = parse_ip(ip_raw)
        if ip_dec == 0:
            continue

        num  = clean(cell(row, "num"), "0")
        wins = clean(cell(row, "w"))
        loss = clean(cell(row, "l"))
        er   = clean(cell(row, "er"))
        bb   = clean(cell(row, "bb"))
        h    = clean(cell(row, "h"))
        so   = clean(cell(row, "so"))
        wl   = f"{wins}-{loss}"

        era  = clean_avg(cell(row, "era")) if col.get("era") else compute_era(er, ip_dec)
        whip = clean_avg(cell(row, "whip")) if col.get("whip") else compute_whip(bb, h, ip_dec)

        pitching_players.append({
            "number": num,
            "player": player_name,
            "ERA":    era,
            "WHIP":   whip,
            "W-L":    wl,
            "IP":     ip_raw,
            "SO":     so,
        })

    print(f"  Parsed {len(pitching_players)} pitching entries.")
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

    soup = BeautifulSoup(html, "lxml")

    print("Parsing batting table...")
    hitting = parse_batting(soup)

    print("Parsing pitching table...")
    pitching = parse_pitching(soup)

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
