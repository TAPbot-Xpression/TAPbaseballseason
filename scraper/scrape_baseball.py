#!/usr/bin/env python3
"""
scraper/scrape_baseball.py
Unified multi-school scraper for Summit League baseball stats & rosters.

Handles:
  1. Sidearm Nuxt 3 & Classic HTML schools (St. Thomas, SDSU, Northern Colorado, Omaha, Oral Roberts)
  2. StatBroadcast stats + Sidearm roster parsing for North Dakota State (NDSU)

Extracts batting & pitching stats, pulls player Position and Year,
normalizes player names to "Firstname Lastname", and outputs individual JSON
files plus a master `all_teams_stats.json` into docs/.

Usage:
  python scraper/scrape_baseball.py
  python scraper/scrape_baseball.py --docs-dir docs --year 2026
  python scraper/scrape_baseball.py --school ndsu
"""

import argparse
import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ── Configured Schools & Sources ──────────────────────────────────────────────
SCHOOLS = [
    {
        "id": "stthomas",
        "name": "University of St. Thomas Tommies",
        "type": "sidearm",
        "stats_url": "https://tommiesports.com/sports/baseball/stats/{year}",
        "roster_url": "https://tommiesports.com/sports/baseball/roster",
        "out_file": "baseball_stats.json",
        "alias_file": "stthomas.json"
    },
    {
        "id": "sdstate",
        "name": "South Dakota State Jackrabbits",
        "type": "sidearm",
        "stats_url": "https://gojacks.com/sports/baseball/stats/{year}",
        "roster_url": "https://gojacks.com/sports/baseball/roster",
        "out_file": "sdstate.json"
    },
    {
        "id": "uncbears",
        "name": "Northern Colorado Bears",
        "type": "sidearm",
        "stats_url": "https://uncbears.com/sports/baseball/stats/{year}",
        "roster_url": "https://uncbears.com/sports/baseball/roster",
        "out_file": "uncbears.json"
    },
    {
        "id": "omaha",
        "name": "Omaha Mavericks",
        "type": "sidearm",
        "stats_url": "https://omahamavs.com/sports/baseball/stats/{year}",
        "roster_url": "https://omahamavs.com/sports/baseball/roster",
        "out_file": "omaha.json"
    },
    {
        "id": "oru",
        "name": "Oral Roberts Golden Eagles",
        "type": "sidearm",
        "stats_url": "https://oruathletics.com/sports/baseball/stats/{year}",
        "roster_url": "https://oruathletics.com/sports/baseball/roster",
        "out_file": "oru.json"
    },
    {
        "id": "ndsu",
        "name": "North Dakota State Bison",
        "type": "statbroadcast",
        "stats_url": "https://stats.gobison.com/custompages/bsb/{year}/teamcume.htm",
        "roster_url": "https://gobison.com/sports/baseball/roster",
        "out_file": "ndsu.json"
    }
]

DEFAULT_DOCS_DIR = "docs"

FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── String & Number Cleaners ───────────────────────────────────────────────────
def clean_val(v, default="0"):
    if v is None or v == -1 or str(v).strip() in ("-1", "", "None", "--", "---", "-.---"):
        return default
    return str(v).strip().replace("\xa0", "").replace("&nbsp;", "")


def clean_avg(v, default=".000"):
    if v is None or str(v).strip() in ("-1", "", "None", "-", "--", "---", "-.---"):
        return default
    return str(v).strip().replace("\xa0", "").replace("&nbsp;", "")


def parse_float(v):
    try:
        return float(str(v).replace("-", "0").replace("\xa0", "").strip())
    except (ValueError, TypeError):
        return 0.0


def parse_ip(ip_str):
    """Handles StatBroadcast IP format: '42.1' = 42 and 1/3 innings."""
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


def format_player_name(raw_name):
    """
    Normalizes 'Lastname, Firstname' or 'Firstname Lastname' into clean 'Firstname Lastname'.
    Strips numeric prefixes, parenthesis, and extra whitespace.
    """
    if not raw_name:
        return ""
    name = str(raw_name).strip()
    name = re.sub(r"\([^)]*\)", "", name)
    name = re.sub(r"^\d+\s+", "", name).strip()
    name = re.sub(r"\s+", " ", name)

    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        last = parts[0]
        first = parts[1] if len(parts) > 1 else ""
        if first:
            return f"{first} {last}".strip()
        return last
    return name


def normalize_name_for_match(raw_name):
    """Returns normalized (first_name, last_name) for fuzzy matching."""
    clean_n = format_player_name(raw_name).lower()
    clean_n = re.sub(r"[^a-z\s]", "", clean_n)
    tokens = clean_n.split()
    if not tokens:
        return "", ""
    if len(tokens) == 1:
        return "", tokens[0]
    return tokens[0], tokens[-1]


# ── Nuxt 3 Resolver ────────────────────────────────────────────────────────────
def resolve_nuxt_payload(raw_json_str):
    try:
        pool = json.loads(raw_json_str)
    except Exception:
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


# ── Roster Parser ──────────────────────────────────────────────────────────────
def parse_roster_page(html):
    """
    Extracts jersey numbers, player names, positions, and academic years
    from Sidearm roster pages across all template formats.
    """
    soup = BeautifulSoup(html, "html.parser")
    roster_by_num = {}
    roster_list = []

    def record_player(num, name, pos, yr):
        cleaned_num = re.sub(r"[^\d]", "", str(num))
        formatted_name = format_player_name(name)
        if formatted_name and (cleaned_num or pos or yr):
            item = {
                "number": cleaned_num if cleaned_num else "999",
                "name": formatted_name,
                "position": str(pos).strip(),
                "year": str(yr).strip()
            }
            roster_list.append(item)
            if cleaned_num:
                roster_by_num[cleaned_num] = item

    # 1. Sidearm Roster Player Cards
    cards = soup.select(".sidearm-roster-player, .sidearm-roster-item, [class*='roster-player-item']")
    for card in cards:
        name_el = card.select_one(".sidearm-roster-player-name, h3, .roster-player-name")
        num_el  = card.select_one(".sidearm-roster-player-jersey-number, .roster-jersey, .sidearm-roster-player-academic-year")
        pos_el  = card.select_one(".sidearm-roster-player-position, .roster-position, .sidearm-roster-player-custom1")
        yr_el   = card.select_one(".sidearm-roster-player-academic-year, .roster-class, .sidearm-roster-player-academic-class")

        name = name_el.get_text(" ", strip=True) if name_el else ""
        num  = num_el.get_text(strip=True) if num_el else ""
        pos  = pos_el.get_text(" ", strip=True) if pos_el else ""
        yr   = yr_el.get_text(" ", strip=True) if yr_el else ""

        if name:
            record_player(num, name, pos, yr)

    # 2. Classic HTML table rows
    if not roster_list:
        for tr in soup.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if len(cells) >= 4:
                row_str = " ".join(cells).lower()
                if "player" in row_str or "pos" in row_str or "year" in row_str or "ht" in row_str:
                    continue
                num = cells[0]
                name = cells[1]
                pos = cells[2] if len(cells) > 2 else ""
                yr = cells[3] if len(cells) > 3 else ""
                if re.sub(r"[^\d]", "", num) and len(name.split()) >= 2:
                    record_player(num, name, pos, yr)

    # 3. Fallback to Nuxt script tag on roster page
    if not roster_list:
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
                        if name and (num or pos):
                            record_player(num, name, pos, yr)
            except Exception:
                pass

    return roster_by_num, roster_list


def match_roster_meta(jersey_num, name, roster_by_num, roster_list):
    """Returns (position, year) matching by uniform number, then fuzzy name."""
    clean_num = str(jersey_num).strip()
    if clean_num in roster_by_num and clean_num not in ("999", ""):
        entry = roster_by_num[clean_num]
        return entry.get("position", ""), entry.get("year", "")

    s_first, s_last = normalize_name_for_match(name)
    if s_last:
        for r in roster_list:
            r_first, r_last = normalize_name_for_match(r["name"])
            if s_last == r_last:
                if not s_first or not r_first or s_first == r_first or s_first.startswith(r_first) or r_first.startswith(s_first):
                    return r.get("position", ""), r.get("year", "")

    return "", ""


# ── Sidearm Extraction (Nuxt 3 & Classic HTML) ────────────────────────────────
def try_extract_nuxt(soup, roster_by_num, roster_list):
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
        raw_name = str(entry.get("playerName", entry.get("name", ""))).strip()

        if not raw_num or not raw_name or raw_num == "-1" or raw_name == "-1":
            continue

        name = format_player_name(raw_name)
        try:
            jersey_int = int(raw_num)
        except ValueError:
            jersey_int = 999

        pos, yr = match_roster_meta(raw_num, name, roster_by_num, roster_list)

        avg = clean_avg(entry.get("battingAverage"))
        ab  = clean_val(entry.get("atBats"))
        r   = clean_val(entry.get("runs"))
        h   = clean_val(entry.get("hits"))
        d2  = clean_val(entry.get("doubles"))
        d3  = clean_val(entry.get("triples"))
        hr  = clean_val(entry.get("homeRuns"))
        rbi = clean_val(entry.get("runsBattedIn"))
        ops = clean_avg(entry.get("ops"))
        slg = clean_avg(entry.get("sluggingPercentage"))

        era  = clean_avg(entry.get("earnedRunAverage"), "-")
        whip = clean_avg(entry.get("whip"), "-")
        wins = clean_val(entry.get("wins"))
        loss = clean_val(entry.get("losses"))
        ip   = clean_val(entry.get("inningsPitched"), "0.0")

        so_raw = (
            entry.get("strikeoutsPitching")
            or entry.get("strikeoutsAllowed")
            or entry.get("pitchingStrikeouts")
            or entry.get("strikeouts")
        )
        so = clean_val(so_raw)
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
                "number":   raw_num,
                "player":   name,
                "position": pos,
                "year":     yr,
                "AVG":      avg,
                "R":        r,
                "H":        h,
                "2B":       d2,
                "3B":       d3,
                "HR":       hr,
                "RBI":      rbi,
                "OPS":      ops,
                "SLG%":     slg,
                "_sort":    jersey_int
            }
            hitting_list.append(bat)
            hitting_by_num[raw_num] = {k: v for k, v in bat.items() if k != "_sort"}

        if has_pitching and raw_num not in pitching_by_num:
            pit = {
                "number":   raw_num,
                "player":   name,
                "position": pos,
                "year":     yr,
                "ERA":      era,
                "WHIP":     whip,
                "W-L":      wl,
                "IP":       ip,
                "SO":       so,
                "_sort":    jersey_int
            }
            pitching_list.append(pit)
            pitching_by_num[raw_num] = {k: v for k, v in pit.items() if k != "_sort"}

        if has_batting or has_pitching:
            p = players_map.get(jersey_int, {
                "jersey_number": jersey_int,
                "player_name":   name,
                "position":      pos,
                "year":          yr
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

    return hitting_list, pitching_list, hitting_by_num, pitching_by_num, players_map


def try_extract_sidearm_html(soup, roster_by_num, roster_list):
    def get_cell(tr, label, default="0"):
        td = tr.find(lambda tag: tag.name in ["td", "th"] and tag.get("data-label", "").lower() == label.lower())
        if td:
            return clean_val(td.get_text(" ", strip=True), default)
        return default

    def extract_jersey_and_name(tr):
        num_span = tr.find("span", class_="mobile-jersey-number")
        raw_num = num_span.get_text(strip=True) if num_span else ""

        if not raw_num:
            num_td = tr.find("td", class_=lambda c: c and "hide-on-medium-down" in c)
            if num_td:
                raw_num = num_td.get_text(strip=True)

        name_tag = tr.find("a", attrs={"data-player-id": True}) or tr.find("th", scope="row") or tr.find("a")
        raw_name = ""
        if name_tag:
            text = name_tag.get_text(" ", strip=True)
            if raw_num and text.startswith(raw_num):
                text = text[len(raw_num):].strip()
            raw_name = text.strip()

        return raw_num, format_player_name(raw_name)

    hitting_list = []
    pitching_list = []
    hitting_by_num = {}
    pitching_by_num = {}
    players_map = {}

    # Batting
    batting_section = soup.find(id="individual-overall-batting") or soup.find(id="overall-batting")
    batting_table = batting_section.find("table") if batting_section else None

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

            pos, yr = match_roster_meta(raw_num, name, roster_by_num, roster_list)

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
                    "number":   raw_num,
                    "player":   name,
                    "position": pos,
                    "year":     yr,
                    "AVG":      avg,
                    "R":        r,
                    "H":        h,
                    "2B":       d2,
                    "3B":       d3,
                    "HR":       hr,
                    "RBI":      rbi,
                    "OPS":      ops,
                    "SLG%":     slg,
                    "_sort":    jersey_int
                }
                hitting_list.append(bat)
                hitting_by_num[raw_num] = {k: v for k, v in bat.items() if k != "_sort"}

                p = players_map.get(jersey_int, {
                    "jersey_number": jersey_int,
                    "player_name":   name,
                    "position":      pos,
                    "year":          yr
                })
                p["batting_stats"] = {
                    "AVG": avg, "R": r, "H": h, "2B": d2, "3B": d3,
                    "HR": hr, "RBI": rbi, "OPS": ops, "SLG%": slg
                }
                players_map[jersey_int] = p

    # Pitching
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

            pos, yr = match_roster_meta(raw_num, name, roster_by_num, roster_list)

            era  = get_cell(tr, "ERA", "-")
            whip = get_cell(tr, "WHIP", "-")
            wl   = get_cell(tr, "W-L", "0-0")
            ip   = get_cell(tr, "IP", "0.0")
            so   = get_cell(tr, "SO", "0")

            if parse_float(ip) > 0 or era != "-":
                pit = {
                    "number":   raw_num,
                    "player":   name,
                    "position": pos,
                    "year":     yr,
                    "ERA":      era,
                    "WHIP":     whip,
                    "W-L":      wl,
                    "IP":       ip,
                    "SO":       so,
                    "_sort":    jersey_int
                }
                pitching_list.append(pit)
                pitching_by_num[raw_num] = {k: v for k, v in pit.items() if k != "_sort"}

                p = players_map.get(jersey_int, {
                    "jersey_number": jersey_int,
                    "player_name":   name,
                    "position":      pos,
                    "year":          yr
                })
                p["pitching_stats"] = {
                    "ERA": era, "WHIP": whip, "W-L": wl, "IP": ip, "SO": so
                }
                players_map[jersey_int] = p

    return hitting_list, pitching_list, hitting_by_num, pitching_by_num, players_map


# ── NDSU StatBroadcast Parser ──────────────────────────────────────────────────
def extract_table_rows(table):
    all_rows = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if any(c.strip() for c in cells):
            all_rows.append(cells)
    return all_rows


def parse_statbroadcast_batting(soup):
    batting_players = []
    target_table = None
    for table in soup.find_all("table"):
        text = table.get_text(" ", strip=True).lower()
        if "avg" in text and "ab" in text and ("rbi" in text or "slg%" in text or "gp-gs" in text):
            target_table = table
            break

    if not target_table:
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
        ab  = clean_val(cell(row, c_ab))
        r   = clean_val(cell(row, c_r))
        h   = clean_val(cell(row, c_h))
        d2  = clean_val(cell(row, c_2b))
        d3  = clean_val(cell(row, c_3b))
        hr  = clean_val(cell(row, c_hr))
        rbi = clean_val(cell(row, c_rbi))
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


def parse_statbroadcast_pitching(soup):
    pitching_players = []
    target_table = None
    for table in soup.find_all("table"):
        text = table.get_text(" ", strip=True).lower()
        if "era" in text and "w-l" in text and "ip" in text and ("er" in text or "bb" in text):
            target_table = table
            break

    if not target_table:
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
        wl   = clean_val(cell(row, c_wl, "0-0"), "0-0")
        h    = clean_val(cell(row, c_h))
        bb   = clean_val(cell(row, c_bb))
        so   = clean_val(cell(row, c_so))
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


def extract_ndsu_stats(stats_html, roster_html, school_meta, year):
    roster_by_num, roster_list = parse_roster_page(roster_html)
    print(f"    [Roster] Parsed {len(roster_list)} players ({len(roster_by_num)} with jersey #s).")

    soup = BeautifulSoup(stats_html, "html.parser")
    raw_batting = parse_statbroadcast_batting(soup)
    raw_pitching = parse_statbroadcast_pitching(soup)

    fallback_id_counter = 1000
    hitting_list = []
    pitching_list = []
    hitting_by_num = {}
    pitching_by_num = {}
    players_map = {}

    def match_roster_player(raw_stat_name):
        s_first, s_last = normalize_name_for_match(raw_stat_name)
        if not s_last:
            return None
        for r in roster_list:
            r_first, r_last = normalize_name_for_match(r["name"])
            if s_last == r_last:
                if not s_first or not r_first or s_first == r_first or s_first.startswith(r_first) or r_first.startswith(s_first):
                    return r
        return None

    for b in raw_batting:
        match = match_roster_player(b["stat_name"])
        if match and match["number"] not in ("999", ""):
            num = match["number"]
            name = match["name"]
            pos = match["position"]
            yr = match["year"]
            jersey_int = int(num) if num.isdigit() else fallback_id_counter
        else:
            fallback_id_counter += 1
            jersey_int = fallback_id_counter
            num = str(jersey_int)
            name = format_player_name(b["stat_name"])
            pos = ""
            yr = ""

        bat = {
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
        hitting_list.append(bat)
        hitting_by_num[num] = {k: v for k, v in bat.items() if k != "_sort"}

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
        match = match_roster_player(p_stat["stat_name"])
        if match and match["number"] not in ("999", ""):
            num = match["number"]
            name = match["name"]
            pos = match["position"]
            yr = match["year"]
            jersey_int = int(num) if num.isdigit() else fallback_id_counter
        else:
            fallback_id_counter += 1
            jersey_int = fallback_id_counter
            num = str(jersey_int)
            name = format_player_name(p_stat["stat_name"])
            pos = ""
            yr = ""

        pit = {
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
        pitching_list.append(pit)
        pitching_by_num[num] = {k: v for k, v in pit.items() if k != "_sort"}

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

    target_url = school_meta["stats_url"].format(year=year)

    return {
        "team_id":            school_meta["id"],
        "team":               school_meta["name"],
        "sport":              "baseball",
        "season":             year,
        "source":             target_url,
        "source_roster":      school_meta["roster_url"],
        "status":             "ok",
        "generated_at":       datetime.now(timezone.utc).isoformat(),
        "players":            players_sorted,
        "hitting":            hitting_list,
        "pitching":           pitching_list,
        "hitting_by_number":  hitting_by_num,
        "pitching_by_number": pitching_by_num,
    }


# ── Unified Extractor Router ──────────────────────────────────────────────────
def extract_stats(stats_html, roster_html, school_meta, year):
    if school_meta.get("type") == "statbroadcast":
        return extract_ndsu_stats(stats_html, roster_html, school_meta, year)

    # Standard Sidearm extraction
    roster_by_num, roster_list = parse_roster_page(roster_html)
    print(f"    [Roster] Parsed {len(roster_list)} players ({len(roster_by_num)} with jersey #s).")

    soup = BeautifulSoup(stats_html, "html.parser")

    res = try_extract_nuxt(soup, roster_by_num, roster_list)
    if res and (res[0] or res[1]):
        print("    [Format] Nuxt 3 JSON payload parsed successfully.")
        hitting_list, pitching_list, hitting_by_num, pitching_by_num, players_map = res
    else:
        print("    [Format] Nuxt payload not found. Falling back to Sidearm HTML table parser...")
        hitting_list, pitching_list, hitting_by_num, pitching_by_num, players_map = try_extract_sidearm_html(soup, roster_by_num, roster_list)

    if not hitting_list and not pitching_list:
        raise ValueError(f"Could not extract batting or pitching stats for {school_meta['name']}.")

    hitting_list.sort(key=lambda x: x.pop("_sort"))
    pitching_list.sort(key=lambda x: x.pop("_sort"))
    players_sorted = [players_map[k] for k in sorted(players_map.keys())]

    target_url = school_meta["stats_url"].format(year=year)

    return {
        "team_id":            school_meta["id"],
        "team":               school_meta["name"],
        "sport":              "baseball",
        "season":             year,
        "source":             target_url,
        "source_roster":      school_meta["roster_url"],
        "status":             "ok",
        "generated_at":       datetime.now(timezone.utc).isoformat(),
        "players":            players_sorted,
        "hitting":            hitting_list,
        "pitching":           pitching_list,
        "hitting_by_number":  hitting_by_num,
        "pitching_by_number": pitching_by_num,
    }


# ── Fetch Helpers ──────────────────────────────────────────────────────────────
def fetch_html_static(url):
    req = urllib.request.Request(url, headers=FETCH_HEADERS)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", errors="replace")


def fetch_html_playwright(page, url):
    print(f"  Navigating to: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_selector(
            "script#__NUXT_DATA__, table.sidearm-table, .sidearm-roster-player, table",
            timeout=20000
        )
    except Exception:
        page.wait_for_timeout(4000)
    return page.content()


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Unified Summit League Baseball Stats & Roster Scraper")
    parser.add_argument("--year", default="2026", help="Season year (e.g. 2026)")
    parser.add_argument("--docs-dir", default=DEFAULT_DOCS_DIR, help="Destination directory for output JSON files")
    parser.add_argument("--school", default=None, help="Scrape only one school ID (stthomas, sdstate, uncbears, omaha, oru, ndsu)")
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
        context = browser.new_context(user_agent=FETCH_HEADERS["User-Agent"])
        page = context.new_page()

        for school in schools_to_scrape:
            stats_url = school["stats_url"].format(year=args.year)
            roster_url = school["roster_url"]
            print(f"\n▶ [{school['id'].upper()}] {school['name']}")
            try:
                # 1. Fetch Stats HTML (static fetch for StatBroadcast, Playwright for Sidearm)
                if school.get("type") == "statbroadcast":
                    print(f"  Fetching static stats: {stats_url}")
                    stats_html = fetch_html_static(stats_url)
                else:
                    stats_html = fetch_html_playwright(page, stats_url)

                # 2. Fetch Roster HTML (Playwright)
                roster_html = fetch_html_playwright(page, roster_url)

                # 3. Extract and Merge
                stats = extract_stats(stats_html, roster_html, school, args.year)

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

    # Save master all_teams_stats.json including NDSU + all other schools
    if len(schools_to_scrape) > 1:
        master_file = os.path.join(args.docs_dir, "all_teams_stats.json")
        with open(master_file, "w", encoding="utf-8") as f:
            json.dump({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "season": args.year,
                "teams": all_teams_data
            }, f, indent=2)
        print(f"\n★ Master file saved: {master_file} ({len(all_teams_data)} teams included)")

    print("\nAll done!")


if __name__ == "__main__":
    main()
