#!/usr/bin/env python3
"""
scraper/scrape_ndsu.py
Scrapes North Dakota State Bison baseball stats from StatBroadcast HTML (teamcume.htm).
Locates tables by matching the specific header markers:
  - Batting: table directly following "Record:" / containing "avg", "ab", "rbi", "slg%"
  - Pitching: table following "(All games Sorted by Earned run avg)" / containing "era", "w-l", "ip"
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


def fetch_html(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", errors="replace")


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
    """Parses '42.1' as 42 + 1/3 innings."""
    try:
        parts = str(ip_str).split(".")
        full = int(parts
[0])
        outs = int(parts
[1]) if len(parts) > 1 else 0
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
    Extracts jersey number from strings like '25 Becker, Colten' or 'Colten Becker (25)'
    or returns ('999', text) if not prefixed with a number.
    """
    text = text.strip()
    # Pattern: "25 Becker, Colten" or "25 Colten Becker"
    m = re.match(r"^(\d{1,2})\s+(.+)$", text)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # Pattern: "Becker, Colten 25"
    m2 = re.search(r"^(.*?)\s+(\d{1,2})$", text)
    if m2:
        return m2.group(2).strip(), m2.group(1).strip()

    return "999", text


def extract_table_rows(table):
    """Extracts raw text cells for each row in a table."""
    all_rows = 
[]
    for tr in table.find_all("tr"):
        cells = 
[td.get_text(" ", strip=True) for td in tr.find_all(
["td", "th"])]
        if any(c.strip() for c in cells):
            all_rows.append(cells)
    return all_rows


# ── Batting Parser ─────────────────────────────────────────────────────────────
def parse_batting_table(soup):
    batting_players = 
[]

    # Find the table that contains batting headers: 'avg', 'ab', 'rbi'
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

    # Locate the header row index
    header_idx = None
    header_cols = 
[]
    for idx, row in enumerate(rows):
        row_lower = 
[c.lower() for c in row]
        if "avg" in row_lower and "ab" in row_lower:
            header_idx = idx
            header_cols = row_lower
            break

    if header_idx is None:
        print("  WARNING: Could not match Batting column headers.")
        return batting_players

    def get_col(candidates):
        for c in candidates:
            for i, h in enumerate(header_cols):
                if h == c.lower():
                    return i
        return None

    c_player = get_col(
["player", "name", "batter"])
    c_avg    = get_col(
["avg"])
    c_ab     = get_col(
["ab"])
    c_r      = get_col(
["r"])
    c_h      = get_col(
["h"])
    c_2b     = get_col(
["2b"])
    c_3b     = get_col(
["3b"])
    c_hr     = get_col(
["hr"])
    c_rbi    = get_col(
["rbi"])
    c_slg    = get_col(
["slg%", "slg"])
    c_obp    = get_col(
["ob%", "obp"])
    c_ops    = get_col(
["ops"])

    for row in rows
[header_idx + 1:]:
        if len(row) <= 2:
            continue

        raw_player = row
[c_player] if c_player is not None and c_player < len(row) else row
[0]
        raw_player = raw_player.strip()

        # Stop / Ignore Totals & Opponents
        if not raw_player or raw_player.lower().startswith("total") or raw_player.lower().startswith("opponent"):
            continue

        # Extract number and name
        num, name = split_jersey_and_name(raw_player)

        avg = clean_avg(row
[c_avg]) if c_avg is not None and c_avg < len(row) else ".000"
        ab  = clean(row
[c_ab]) if c_ab is not None and c_ab < len(row) else "0"
        r   = clean(row
[c_r]) if c_r is not None and c_r < len(row) else "0"
        h   = clean(row
[c_h]) if c_h is not None and c_h < len(row) else "0"
        d2  = clean(row
[c_2b]) if c_2b is not None and c_2b < len(row) else "0"
        d3  = clean(row
[c_3b]) if c_3b is not None and c_3b < len(row) else "0"
        hr  = clean(row
[c_hr]) if c_hr is not None and c_hr < len(row) else "0"
        rbi = clean(row
[c_rbi]) if c_rbi is not None and c_rbi < len(row) else "0"
        slg = clean_avg(row
[c_slg]) if c_slg is not None and c_slg < len(row) else ".000"
        obp = clean_avg(row
[c_obp]) if c_obp is not None and c_obp < len(row) else ".000"

        ops = clean_avg(row
[c_ops]) if c_ops is not None and c_ops < len(row) else compute_ops(obp, slg)

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
    pitching_players = 
[]

    # Find the table that contains pitching headers: 'era', 'w-l', 'ip', 'so'
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

    header_idx = None
    header_cols = 
[]
    for idx, row in enumerate(rows):
        row_lower = 
[c.lower() for c in row]
        if "era" in row_lower and "ip" in row_lower and "w-l" in row_lower:
            header_idx = idx
            header_cols = row_lower
            break

    if header_idx is None:
        print("  WARNING: Could not match Pitching column headers.")
        return pitching_players

    def get_col(candidates):
        for c in candidates:
            for i, h in enumerate(header_cols):
                if h == c.lower():
                    return i
        return None

    c_player = get_col(
["player", "name", "pitcher"])
    c_era    = get_col(
["era"])
    c_wl     = get_col(
["w-l", "w-l-s", "wl"])
    c_ip     = get_col(
["ip"])
    c_h      = get_col(
["h"])
    c_bb     = get_col(
["bb"])
    c_so     = get_col(
["so", "k"])
    c_whip   = get_col(
["whip"])

    for row in rows
[header_idx + 1:]:
        if len(row) <= 2:
            continue

        raw_player = row
[c_player] if c_player is not None and c_player < len(row) else row
[0]
        raw_player = raw_player.strip()

        # Stop / Ignore Totals & Opponents
        if not raw_player or raw_player.lower().startswith("total") or raw_player.lower().startswith("opponent"):
            continue

        num, name = split_jersey_and_name(raw_player)

        era  = clean_avg(row
[c_era]) if c_era is not None and c_era < len(row) else "-"
        wl   = clean(row
[c_wl], "0-0") if c_wl is not None and c_wl < len(row) else "0-0"
        ip   = clean(row
[c_ip], "0.0") if c_ip is not None and c_ip < len(row) else "0.0"
        h    = clean(row
[c_h], "0") if c_h is not None and c_h < len(row) else "0"
        bb   = clean(row
[c_bb], "0") if c_bb is not None and c_bb < len(row) else "0"
        so   = clean(row
[c_so], "0") if c_so is not None and c_so < len(row) else "0"

        whip = clean_avg(row
[c_whip]) if c_whip is not None and c_whip < len(row) else compute_whip(bb, h, ip)

        if parse_float(ip) > 0 or era != "-":
            pitching_players.append({
                "number": num,
                "player": name,
                "ERA":    era,
                "WHIP":   whip,
                "W-L":    wl,
                "IP":     ip,
                "SO":     so,
            })

    return pitching_players


# ── Unified Player Combiner ───────────────────────────────────────────────────
def build_players(hitting, pitching):
    players_map = {}

    for entry in hitting:
        try:
            jersey_int = int(entry
["number"])
        except ValueError:
            jersey_int = 999

        p = players_map.get(jersey_int, {
            "jersey_number": jersey_int,
            "player_name":   entry
["player"]
        })
        p
["batting_stats"] = {
            "AVG":  entry
["AVG"],
            "R":    entry
["R"],
            "H":    entry
["H"],
            "2B":   entry
["2B"],
            "3B":   entry
["3B"],
            "HR":   entry
["HR"],
            "RBI":  entry
["RBI"],
            "OPS":  entry
["OPS"],
            "SLG%": entry
["SLG%"],
        }
        players_map
[jersey_int] = p

    for entry in pitching:
        try:
            jersey_int = int(entry
["number"])
        except ValueError:
            jersey_int = 999

        p = players_map.get(jersey_int, {
            "jersey_number": jersey_int,
            "player_name":   entry
["player"]
        })
        p
["pitching_stats"] = {
            "ERA":  entry
["ERA"],
            "WHIP": entry
["WHIP"],
            "W-L":  entry
["W-L"],
            "IP":   entry
["IP"],
            "SO":   entry
["SO"],
        }
        players_map
[jersey_int] = p

    return 
[players_map
[k] for k in sorted(players_map.keys())]


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

    print("Parsing pitching table...")
    pitching = parse_pitching_table(soup)

    hitting_by_num  = {p
["number"]: p for p in hitting}
    pitching_by_num = {p
["number"]: p for p in pitching}
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
