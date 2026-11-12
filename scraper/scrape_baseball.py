#!/usr/bin/env python3
"""
scraper/scrape_baseball.py
Unified multi-school scraper for Summit League baseball stats & rosters.

Fixes applied:
  1. Fixes Name <-> Position column swap on print-roster parsing.
  2. Filters out sub-split duplicate rows (preventing the 120+ duplicate batter/pitcher issue).
  3. Corrects Oral Roberts HTML table parsing & player identification.
  4. Supports St. Thomas, SDSU, Northern Colorado, Omaha, Oral Roberts, NDSU, and Non-Conf.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import logging
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext, Page, async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("baseball")
sys.setrecursionlimit(20000)

DEFAULT_DOCS_DIR = "docs"
NON_CONF_CONFIG_PATH = "non_conf_config.json"
NON_CONF_ID = "non_conf"
NON_CONF_FILE = "non_conf.json"
CONCURRENCY_LIMIT = 4

# Stat field keys
BATTING_FIELDS = ["AVG", "R", "H", "2B", "3B", "HR", "RBI", "OPS", "SLG%"]
PITCHING_FIELDS = ["ERA", "WHIP", "W-L", "IP", "SO"]

# ── Summit League Baseball Programs ───────────────────────────────────────────
TEAMS = [
    {
        "id": "stthomas",
        "name": "St. Thomas Tommies",
        "type": "sidearm",
        "stats_url": "https://tommiesports.com/sports/baseball/stats/{year}",
        "roster_url": "https://tommiesports.com/sports/baseball/roster/print",
        "out_file": "stthomas.json",
        "alias_file": "baseball_stats.json"
    },
    {
        "id": "sdstate",
        "name": "South Dakota State Jackrabbits",
        "type": "sidearm",
        "stats_url": "https://gojacks.com/sports/baseball/stats/{year}",
        "roster_url": "https://gojacks.com/sports/baseball/roster/print",
        "out_file": "sdstate.json"
    },
    {
        "id": "uncbears",
        "name": "Northern Colorado Bears",
        "type": "sidearm",
        "stats_url": "https://uncbears.com/sports/baseball/stats/{year}",
        "roster_url": "https://uncbears.com/sports/baseball/roster/print",
        "out_file": "uncbears.json"
    },
    {
        "id": "omaha",
        "name": "Omaha Mavericks",
        "type": "sidearm",
        "stats_url": "https://omahamavs.com/sports/baseball/stats/{year}",
        "roster_url": "https://omahamavs.com/sports/baseball/roster/print",
        "out_file": "omaha.json"
    },
    {
        "id": "oru",
        "name": "Oral Roberts Golden Eagles",
        "type": "sidearm",
        "stats_url": "https://oruathletics.com/sports/baseball/stats/{year}",
        "roster_url": "https://oruathletics.com/sports/baseball/roster?print=true",
        "out_file": "oru.json"
    },
    {
        "id": "ndsu",
        "name": "North Dakota State Bison",
        "type": "statbroadcast",
        "stats_url": "https://stats.gobison.com/custompages/bsb/{year}/teamcume.htm",
        "fallback_stats_url": "https://gobison.com/sports/baseball/stats/{year}",
        "roster_url": "https://gobison.com/sports/baseball/roster/print",
        "out_file": "ndsu.json"
    }
]

FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Cleaners & Evaluators ─────────────────────────────────────────────────────
_BLANK = {"", "-1", "none", "null", "nan", "n/a", "na", "-", "--", "---", "-.---", "—"}

def _s(v: Any) -> str:
    if v is None or isinstance(v, bool):
        return ""
    return str(v).replace("\xa0", " ").strip()

def clean_val(v: Any, default: str = "0") -> str:
    if v is None or _s(v).lower() in _BLANK:
        return default
    s = _s(v).replace(",", "")
    try:
        return str(int(round(float(s))))
    except ValueError:
        m = re.match(r"^(\d+)", s)
        return m.group(1) if m else default

def clean_avg(v: Any, default: str = ".000") -> str:
    s = _s(v).replace(" ", "")
    if s.lower() in _BLANK:
        return default
    try:
        f = float(s)
    except ValueError:
        return default
    if f < 1:
        return f"{f:.3f}".lstrip("0") if f > 0 else ".000"
    return f"{f:.3f}"

def clean_rate(v: Any, default: str = "-") -> str:
    s = _s(v).replace(" ", "")
    if s.lower() in _BLANK:
        return default
    if s.lower() in ("inf", "infinity", "∞"):
        return "INF"
    try:
        return f"{float(s):.2f}"
    except ValueError:
        return default

def clean_ip(v: Any, default: str = "0.0") -> str:
    s = _s(v).replace(" ", "")
    if s.lower() in _BLANK:
        return default
    if re.fullmatch(r"\d+", s):
        return s + ".0"
    m = re.match(r"^(\d+)\.(\d)", s)
    return f"{m.group(1)}.{m.group(2)}" if m else default

def clean_wl(v: Any) -> str:
    s = _s(v).replace(" ", "")
    return s if re.fullmatch(r"\d+-\d+", s) else "0-0"

def parse_float(v: Any) -> float:
    try:
        return float(_s(v).replace(",", "").replace("-", "0"))
    except ValueError:
        return 0.0

def parse_ip(v: Any) -> float:
    m = re.match(r"^(\d+)(?:\.(\d))?", _s(v))
    if not m:
        return 0.0
    return int(m.group(1)) + min(int(m.group(2) or 0), 2) / 3.0

def compute_ops(obp: Any, slg: Any) -> str:
    return clean_avg(parse_float(obp) + parse_float(slg))

def compute_whip(bb: Any, h: Any, ip: Any) -> str:
    innings = parse_ip(ip)
    if innings <= 0:
        return "-"
    return f"{(parse_float(bb) + parse_float(h)) / innings:.2f}"

# ── Name, Number, Position & Academic Year Helpers ─────────────────────────────
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_POS_TOKENS = {
    "P", "RHP", "LHP", "C", "1B", "2B", "3B", "SS", "IF", "INF", "MIF", "CIF",
    "OF", "LF", "CF", "RF", "DH", "UT", "UTL", "UTIL",
}
_POS_WORDS = {
    "PITCHER": "P", "CATCHER": "C", "INFIELDER": "INF", "OUTFIELDER": "OF", "UTILITY": "UTIL",
    "RIGHTHANDEDPITCHER": "RHP", "LEFTHANDEDPITCHER": "LHP", "FIRSTBASE": "1B", "SECONDBASE": "2B",
    "THIRDBASE": "3B", "SHORTSTOP": "SS",
}

_CLASS_BASE = {
    "FR": "Fr.", "FRESH": "Fr.", "FRESHMAN": "Fr.", "FIRSTYEAR": "Fr.", "1ST": "Fr.", "1STYEAR": "Fr.", "1": "Fr.",
    "SO": "So.", "SOPH": "So.", "SOPHOMORE": "So.", "SECONDYEAR": "So.", "2ND": "So.", "2NDYEAR": "So.", "2": "So.",
    "JR": "Jr.", "JUNIOR": "Jr.", "THIRDYEAR": "Jr.", "3RD": "Jr.", "3RDYEAR": "Jr.", "3": "Jr.",
    "SR": "Sr.", "SENIOR": "Sr.", "FOURTHYEAR": "Sr.", "4TH": "Sr.", "4THYEAR": "Sr.", "4": "Sr.",
    "GR": "Gr.", "GRAD": "Gr.", "GRADUATE": "Gr.", "GRADSTUDENT": "Gr.", "GRADUATESTUDENT": "Gr.",
    "5TH": "Gr.", "5THYEAR": "Gr.", "FIFTHYEAR": "Gr.", "5": "Gr.", "6TH": "Gr.",
}

def format_player_name(raw: Any) -> str:
    """Normalizes 'Lastname, Firstname' or 'Firstname Lastname' into clean 'Firstname Lastname'."""
    if not raw:
        return ""
    name = _s(raw)
    name = re.sub(r"(?i)\bview bio\b|\bimage\b|\bjersey number\b", " ", name)
    name = re.sub(r"\([^)]*\)", " ", name)
    name = name.replace('"', " ").replace("\u201c", " ").replace("\u201d", " ")
    name = re.sub(r"#\s*\d+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    if not name or "@" in name or "//" in name:
        return ""

    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        last = parts[0]
        first = parts[1] if len(parts) > 1 else ""
        if re.sub(r"[^a-z]", "", first.lower()) in _SUFFIXES:
            name = f"{last} {first}"
        elif first:
            name = f"{first} {last}"
        else:
            name = last

    # Remove duplicated glued strings e.g. "John SmithJohn Smith"
    words = name.split()
    half = len(words) // 2
    if len(words) >= 4 and len(words) % 2 == 0 and words[:half] == words[half:]:
        name = " ".join(words[:half])

    return name.title() if name.isupper() else name

def is_valid_player_name(name: Any) -> bool:
    n = _s(name)
    if len(n) < 3 or not re.search(r"[A-Za-z]", n):
        return False
    low = n.lower()
    if any(k in low for k in ("//", "totals", "opponents", "team", "score", "game", "date")):
        return False
    if re.fullmatch(r"[\d.\-/%\s]+", low):
        return False
    return True

def normalize_name_for_match(raw_name: Any) -> Tuple[str, str]:
    clean_n = format_player_name(raw_name).lower()
    clean_n = re.sub(r"[^a-z\s]", "", clean_n)
    tokens = [t for t in clean_n.split() if t not in _SUFFIXES]
    if not tokens:
        return "", ""
    if len(tokens) == 1:
        return "", tokens[0]
    return tokens[0], tokens[-1]

def normalize_position(s: Any) -> str:
    t = _s(s).upper().replace(".", "")
    if not t:
        return ""
    t = re.sub(r"\s*[/,&]\s*", "/", t)
    parts = [p for p in t.split("/") if p.strip()]
    out: List[str] = []
    for p in parts:
        key = re.sub(r"[^A-Z0-9]", "", p)
        if key in _POS_TOKENS:
            out.append(key)
        elif key in _POS_WORDS:
            out.append(_POS_WORDS[key])
        else:
            return ""
    return "/".join(out)

def normalize_year(s: Any) -> str:
    y = re.sub(r"[^A-Z0-9]", "", _s(s).upper())
    if not y:
        return ""
    if y in _CLASS_BASE:
        return _CLASS_BASE[y]
    for prefix in ("REDSHIRT", "RS", "R"):
        if y.startswith(prefix):
            base = _CLASS_BASE.get(y[len(prefix):])
            if base:
                return base if base == "Gr." else f"R-{base}"
    return ""

# ── Nuxt 3 Resolver ────────────────────────────────────────────────────────────
_NUXT_WRAPPERS = {"Reactive", "ShallowReactive", "Ref", "ShallowRef", "EmptyRef", "EmptyShallowRef"}

def resolve_nuxt_payload(raw_json_str: str) -> Optional[List[Any]]:
    try:
        pool = json.loads(raw_json_str)
    except Exception:
        return None
    if not isinstance(pool, list):
        return pool

    memo: Dict[int, Any] = {}

    def resolve(idx: Any) -> Any:
        if not isinstance(idx, int) or isinstance(idx, bool):
            return idx
        if idx < 0 or idx >= len(pool):
            return None
        if idx in memo:
            return memo[idx]
        node = pool[idx]
        if isinstance(node, dict):
            out: Dict[str, Any] = {}
            memo[idx] = out
            for k, v in node.items():
                out[k] = resolve(v)
            return out
        if isinstance(node, list):
            if len(node) == 2 and isinstance(node[0], str) and node[0] in _NUXT_WRAPPERS:
                memo[idx] = None
                res = resolve(node[1])
                memo[idx] = res
                return res
            lst: List[Any] = []
            memo[idx] = lst
            for v in node:
                lst.append(resolve(v))
            return lst
        memo[idx] = node
        return node

    return [resolve(i) for i in range(len(pool))]

def iter_dicts(root: Any) -> Iterator[Dict[str, Any]]:
    stack = [root]
    seen: set = set()
    while stack:
        o = stack.pop()
        if isinstance(o, (dict, list)):
            if id(o) in seen:
                continue
            seen.add(id(o))
            if isinstance(o, dict):
                yield o
                stack.extend(o.values())
            else:
                stack.extend(o)

# ── Roster Parser ──────────────────────────────────────────────────────────────
def parse_roster_page(html: str) -> Tuple[Dict[str, Dict[str, str]], List[Dict[str, str]]]:
    soup = BeautifulSoup(html, "html.parser")
    roster_by_num: Dict[str, Dict[str, str]] = {}
    roster_list: List[Dict[str, str]] = []

    def record_player(num: Any, name: Any, pos: Any, yr: Any):
        cleaned_num = re.sub(r"[^\d]", "", _s(num))
        formatted_name = format_player_name(name)
        norm_pos = normalize_position(pos)
        norm_yr = normalize_year(yr)

        # Disallow inverted assignments
        if not norm_pos and normalize_position(yr):
            norm_pos = normalize_position(yr)
        if not norm_yr and normalize_year(pos):
            norm_yr = normalize_year(pos)

        if is_valid_player_name(formatted_name) and (cleaned_num or norm_pos or norm_yr):
            item = {
                "number": cleaned_num if cleaned_num else "999",
                "name": formatted_name,
                "position": norm_pos,
                "year": norm_yr
            }
            roster_list.append(item)
            if cleaned_num:
                roster_by_num[cleaned_num] = item

    # 1. Check Classic HTML Table rows
    for tr in soup.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if len(cells) >= 3:
            row_str = " ".join(cells).lower()
            if any(h in row_str for h in ["hometown", "high school", "wt", "ht", "b/t", "player", "pos"]):
                # Classify cells by actual content type
                c_num, c_name, c_pos, c_yr = "", "", "", ""
                for c in cells:
                    if not c_num and re.fullmatch(r"#?\d{1,2}", c):
                        c_num = c
                    elif not c_pos and normalize_position(c):
                        c_pos = c
                    elif not c_yr and normalize_year(c):
                        c_yr = c
                    elif not c_name and is_valid_player_name(c) and len(c.split()) >= 2:
                        c_name = c
                if c_name:
                    record_player(c_num, c_name, c_pos, c_yr)

    # 2. Sidearm Roster Player Cards
    if len(roster_list) < 5:
        cards = soup.select(".sidearm-roster-player, .sidearm-roster-item, [class*='roster-player-item']")
        for card in cards:
            name_el = card.select_one(".sidearm-roster-player-name, h3, .roster-player-name")
            num_el = card.select_one(".sidearm-roster-player-jersey-number, .roster-jersey")
            pos_el = card.select_one(".sidearm-roster-player-position, .roster-position")
            yr_el = card.select_one(".sidearm-roster-player-academic-year, .roster-class")

            name = name_el.get_text(" ", strip=True) if name_el else ""
            num = num_el.get_text(strip=True) if num_el else ""
            pos = pos_el.get_text(" ", strip=True) if pos_el else ""
            yr = yr_el.get_text(" ", strip=True) if yr_el else ""
            if name:
                record_player(num, name, pos, yr)

    # 3. Fallback to Nuxt script tag on roster page
    if len(roster_list) < 5:
        tag = soup.find("script", id="__NUXT_DATA__")
        if tag and tag.string:
            pool = resolve_nuxt_payload(tag.string.strip())
            if pool:
                for rec in iter_dicts(pool):
                    num = _s(rec.get("jerseyNumber", rec.get("uniform", "")))
                    name = _s(rec.get("playerName", rec.get("name", "")))
                    pos = _s(rec.get("positionShort", rec.get("position", "")))
                    yr = _s(rec.get("academicYearShort", rec.get("academicYear", "")))
                    if name and (num or pos):
                        record_player(num, name, pos, yr)

    return roster_by_num, roster_list

def match_roster_meta(jersey_num: str, name: str, roster_by_num: Dict[str, Any], roster_list: List[Dict[str, Any]]) -> Tuple[str, str, str]:
    clean_num = _s(jersey_num).strip()
    if clean_num in roster_by_num and clean_num not in ("999", ""):
        entry = roster_by_num[clean_num]
        return entry.get("name", name), entry.get("position", ""), entry.get("year", "")

    s_first, s_last = normalize_name_for_match(name)
    if s_last:
        for r in roster_list:
            r_first, r_last = normalize_name_for_match(r["name"])
            if s_last == r_last:
                if not s_first or not r_first or s_first == r_first or s_first.startswith(r_first) or r_first.startswith(s_first):
                    return r.get("name", name), r.get("position", ""), r.get("year", "")

    return format_player_name(name), "", ""

# ── Nuxt 3 Stats Extractor ─────────────────────────────────────────────────────
def try_extract_nuxt(soup: BeautifulSoup, roster_by_num: Dict[str, Any], roster_list: List[Dict[str, Any]]) -> Optional[Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any], Dict[str, Any], Dict[int, Any]]]:
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

    entries = []
    for rec in iter_dicts(resolved_pool):
        lst = rec.get("overallIndividualStats")
        if isinstance(lst, list):
            entries.extend(e for e in lst if isinstance(e, dict))

    if not entries:
        entries = [rec for rec in iter_dicts(resolved_pool) if "playerUniform" in rec]

    if not entries:
        return None

    hitting_map: Dict[str, Dict[str, Any]] = {}
    pitching_map: Dict[str, Dict[str, Any]] = {}
    players_map: Dict[int, Any] = {}

    for entry in entries:
        if entry.get("isAFooterStat") == 1 or entry.get("isAFooterStat") is True:
            continue

        raw_num = _s(entry.get("playerUniform", ""))
        raw_name = _s(entry.get("playerName", entry.get("name", "")))
        if not raw_name or not is_valid_player_name(raw_name) or raw_name == "-1":
            continue

        clean_name, pos, yr = match_roster_meta(raw_num, raw_name, roster_by_num, roster_list)
        try:
            jersey_int = int(raw_num) if raw_num.isdigit() else 999
        except ValueError:
            jersey_int = 999

        avg = clean_avg(entry.get("battingAverage"))
        ab = clean_val(entry.get("atBats"))
        r = clean_val(entry.get("runs"))
        h = clean_val(entry.get("hits"))
        d2 = clean_val(entry.get("doubles"))
        d3 = clean_val(entry.get("triples"))
        hr = clean_val(entry.get("homeRuns"))
        rbi = clean_val(entry.get("runsBattedIn"))
        ops = clean_avg(entry.get("ops"))
        slg = clean_avg(entry.get("sluggingPercentage"))

        era = clean_rate(entry.get("earnedRunAverage"))
        whip = clean_rate(entry.get("whip"))
        wins = clean_val(entry.get("wins"))
        loss = clean_val(entry.get("losses"))
        ip = clean_ip(entry.get("inningsPitched"))
        so_raw = entry.get("strikeoutsPitching") or entry.get("strikeoutsAllowed") or entry.get("pitchingStrikeouts") or entry.get("strikeouts")
        so = clean_val(so_raw)
        wl = f"{wins}-{loss}"

        has_batting = parse_float(ab) > 0 or parse_float(h) > 0 or parse_float(r) > 0 or (avg != ".000" and avg != "-")
        has_pitching = parse_ip(ip) > 0 or era != "-" or wins != "0" or loss != "0"

        player_key = f"{jersey_int}_{clean_name.lower()}"

        # Deduplicate and keep maximum volume season total line
        if has_batting:
            cur_bat = hitting_map.get(player_key)
            if cur_bat is None or parse_float(ab) > parse_float(cur_bat.get("AB", 0)):
                hitting_map[player_key] = {
                    "number": str(jersey_int) if jersey_int != 999 else raw_num,
                    "player": clean_name,
                    "position": pos,
                    "year": yr,
                    "AVG": avg,
                    "R": r,
                    "H": h,
                    "2B": d2,
                    "3B": d3,
                    "HR": hr,
                    "RBI": rbi,
                    "OPS": ops,
                    "SLG%": slg,
                    "AB": ab,
                    "_sort": jersey_int
                }

        if has_pitching:
            cur_pit = pitching_map.get(player_key)
            if cur_pit is None or parse_ip(ip) > parse_ip(cur_pit.get("IP", "0.0")):
                pitching_map[player_key] = {
                    "number": str(jersey_int) if jersey_int != 999 else raw_num,
                    "player": clean_name,
                    "position": pos or "P",
                    "year": yr,
                    "ERA": era,
                    "WHIP": whip,
                    "W-L": wl,
                    "IP": ip,
                    "SO": so,
                    "_sort": jersey_int
                }

        if has_batting or has_pitching:
            p = players_map.get(jersey_int, {
                "jersey_number": jersey_int,
                "player_name": clean_name,
                "position": pos,
                "year": yr
            })
            if has_batting:
                p["batting_stats"] = {
                    "AVG": avg, "R": r, "H": h, "2B": d2, "3B": d3, "HR": hr, "RBI": rbi, "OPS": ops, "SLG%": slg
                }
            if has_pitching:
                p["pitching_stats"] = {
                    "ERA": era, "WHIP": whip, "W-L": wl, "IP": ip, "SO": so
                }
            players_map[jersey_int] = p

    hitting_list = list(hitting_map.values())
    pitching_list = list(pitching_map.values())

    for b in hitting_list:
        b.pop("AB", None)

    hitting_by_num = {b["number"]: {k: v for k, v in b.items() if k != "_sort"} for b in hitting_list}
    pitching_by_num = {p["number"]: {k: v for k, v in p.items() if k != "_sort"} for p in pitching_list}

    return hitting_list, pitching_list, hitting_by_num, pitching_by_num, players_map

# ── Sidearm Classic HTML Table Parser (Oral Roberts Fix) ──────────────────────
def try_extract_sidearm_html(soup: BeautifulSoup, roster_by_num: Dict[str, Any], roster_list: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any], Dict[str, Any], Dict[int, Any]]:
    hitting_list, pitching_list = [], []
    hitting_by_num, pitching_by_num = {}, {}
    players_map = {}

    def extract_rows(table: Any, is_pitching: bool = False):
        if not table:
            return []
        rows_data = []
        thead = table.find("thead")
        header_cols = []
        if thead:
            last_head = thead.find_all("tr")[-1]
            header_cols = [th.get_text(" ", strip=True).upper() for th in last_head.find_all(["th", "td"])]

        tbody = table.find("tbody") or table
        for tr in tbody.find_all("tr"):
            if "footer_row" in (tr.get("class") or []) or tr.find("th", attrs={"colspan": True}):
                continue
            cells = tr.find_all(["td", "th"])
            if len(cells) < 4:
                continue

            num_span = tr.find("span", class_="mobile-jersey-number")
            raw_num = num_span.get_text(strip=True) if num_span else ""
            if not raw_num:
                num_td = tr.find("td", class_=lambda c: c and "hide-on-medium-down" in c)
                if num_td:
                    raw_num = num_td.get_text(strip=True)

            name_tag = tr.find("a", attrs={"data-player-id": True}) or tr.find("th", scope="row") or tr.find("a")
            raw_name = name_tag.get_text(" ", strip=True) if name_tag else ""

            if raw_num and raw_name.startswith(raw_num):
                raw_name = raw_name[len(raw_num):].strip()

            raw_name = format_player_name(raw_name)
            if not is_valid_player_name(raw_name):
                continue

            # Build cell label map
            cell_map = {}
            for idx, c in enumerate(cells):
                label = (c.get("data-label") or (header_cols[idx] if idx < len(header_cols) else "")).strip().upper()
                if label:
                    cell_map[label] = c.get_text(" ", strip=True)

            clean_name, pos, yr = match_roster_meta(raw_num, raw_name, roster_by_num, roster_list)
            try:
                jersey_int = int(raw_num) if raw_num.isdigit() else 999
            except ValueError:
                jersey_int = 999

            if not is_pitching:
                avg = clean_avg(cell_map.get("AVG"))
                ab = clean_val(cell_map.get("AB"))
                r = clean_val(cell_map.get("R"))
                h = clean_val(cell_map.get("H"))
                d2 = clean_val(cell_map.get("2B"))
                d3 = clean_val(cell_map.get("3B"))
                hr = clean_val(cell_map.get("HR"))
                rbi = clean_val(cell_map.get("RBI"))
                ops = clean_avg(cell_map.get("OPS"))
                slg = clean_avg(cell_map.get("SLG%"))
                if parse_float(ab) > 0 or parse_float(h) > 0 or parse_float(r) > 0:
                    rows_data.append({
                        "number": str(jersey_int) if jersey_int != 999 else raw_num,
                        "player": clean_name, "position": pos, "year": yr,
                        "AVG": avg, "R": r, "H": h, "2B": d2, "3B": d3, "HR": hr, "RBI": rbi, "OPS": ops, "SLG%": slg,
                        "_sort": jersey_int
                    })
            else:
                era = clean_rate(cell_map.get("ERA"))
                whip = clean_rate(cell_map.get("WHIP"))
                wl = clean_wl(cell_map.get("W-L"))
                ip = clean_ip(cell_map.get("IP"))
                so = clean_val(cell_map.get("SO"))
                if parse_ip(ip) > 0 or era != "-":
                    rows_data.append({
                        "number": str(jersey_int) if jersey_int != 999 else raw_num,
                        "player": clean_name, "position": pos or "P", "year": yr,
                        "ERA": era, "WHIP": whip, "W-L": wl, "IP": ip, "SO": so,
                        "_sort": jersey_int
                    })
        return rows_data

    # Find Batting Table
    bat_sec = soup.find(id="individual-overall-batting") or soup.find(id="overall-batting")
    bat_tbl = bat_sec.find("table") if bat_sec else None
    if not bat_tbl:
        for t in soup.find_all("table"):
            cap = t.find("caption")
            if cap and "batting" in cap.get_text().lower() and "overall" in cap.get_text().lower():
                bat_tbl = t
                break
    hitting_list = extract_rows(bat_tbl, is_pitching=False)

    # Find Pitching Table
    pit_sec = soup.find(id="individual-overall-pitching") or soup.find(id="overall-pitching")
    pit_tbl = pit_sec.find("table") if pit_sec else None
    if not pit_tbl:
        for t in soup.find_all("table"):
            cap = t.find("caption")
            if cap and "pitching" in cap.get_text().lower() and "overall" in cap.get_text().lower():
                pit_tbl = t
                break
    pitching_list = extract_rows(pit_tbl, is_pitching=True)

    for b in hitting_list:
        j = b["_sort"]
        hitting_by_num[b["number"]] = {k: v for k, v in b.items() if k != "_sort"}
        p = players_map.get(j, {"jersey_number": j, "player_name": b["player"], "position": b["position"], "year": b["year"]})
        p["batting_stats"] = {k: b[k] for k in BATTING_FIELDS}
        players_map[j] = p

    for pit in pitching_list:
        j = pit["_sort"]
        pitching_by_num[pit["number"]] = {k: v for k, v in pit.items() if k != "_sort"}
        p = players_map.get(j, {"jersey_number": j, "player_name": pit["player"], "position": pit["position"], "year": pit["year"]})
        p["pitching_stats"] = {k: pit[k] for k in PITCHING_FIELDS}
        players_map[j] = p

    return hitting_list, pitching_list, hitting_by_num, pitching_by_num, players_map

# ── StatBroadcast Parser (NDSU) ────────────────────────────────────────────────
def parse_statbroadcast(html: str, roster_by_num: Dict[str, Any], roster_list: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any], Dict[str, Any], Dict[int, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    hitting_list, pitching_list = [], []
    hitting_by_num, pitching_by_num = {}, {}
    players_map = {}

    for table in soup.find_all("table"):
        text = table.get_text(" ", strip=True).lower()
        rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])] for tr in table.find_all("tr")]
        if not rows:
            continue

        header_idx = None
        for i, r in enumerate(rows):
            r_low = [c.lower() for c in r]
            if "avg" in r_low and "ab" in r_low:
                header_idx = i
                break

        if header_idx is not None:
            cols = [c.lower() for c in rows[header_idx]]
            def col(names):
                for n in names:
                    if n in cols:
                        return cols.index(n)
                return None

            c_p, c_avg, c_ab, c_r, c_h = col(["player", "name"]), col(["avg"]), col(["ab"]), col(["r"]), col(["h"])
            c_2b, c_3b, c_hr, c_rbi = col(["2b"]), col(["3b"]), col(["hr"]), col(["rbi"])
            c_slg, c_obp, c_ops = col(["slg%", "slg"]), col(["ob%", "obp"]), col(["ops"])

            for r in rows[header_idx + 1:]:
                if len(r) <= 3:
                    continue
                raw_name = r[c_p] if c_p is not None and c_p < len(r) else r[0]
                if not is_valid_player_name(raw_name):
                    continue
                clean_name, pos, yr = match_roster_meta("", raw_name, roster_by_num, roster_list)
                num = next((p["number"] for p in roster_list if p["name"].lower() == clean_name.lower()), "")
                try:
                    jersey_int = int(num) if num.isdigit() else 999
                except ValueError:
                    jersey_int = 999

                ab = clean_val(r[c_ab]) if c_ab else "0"
                h = clean_val(r[c_h]) if c_h else "0"
                rn = clean_val(r[c_r]) if c_r else "0"
                if parse_float(ab) > 0 or parse_float(h) > 0 or parse_float(rn) > 0:
                    ops = clean_avg(r[c_ops]) if c_ops else compute_ops(r[c_obp] if c_obp else "0", r[c_slg] if c_slg else "0")
                    bat = {
                        "number": num, "player": clean_name, "position": pos, "year": yr,
                        "AVG": clean_avg(r[c_avg]) if c_avg else ".000",
                        "R": rn, "H": h, "2B": clean_val(r[c_2b]) if c_2b else "0",
                        "3B": clean_val(r[c_3b]) if c_3b else "0", "HR": clean_val(r[c_hr]) if c_hr else "0",
                        "RBI": clean_val(r[c_rbi]) if c_rbi else "0", "OPS": ops,
                        "SLG%": clean_avg(r[c_slg]) if c_slg else ".000",
                        "_sort": jersey_int
                    }
                    hitting_list.append(bat)
                    hitting_by_num[num] = {k: v for k, v in bat.items() if k != "_sort"}
                    p = players_map.get(jersey_int, {"jersey_number": jersey_int, "player_name": clean_name, "position": pos, "year": yr})
                    p["batting_stats"] = {k: bat[k] for k in BATTING_FIELDS}
                    players_map[jersey_int] = p

    return hitting_list, pitching_list, hitting_by_num, pitching_by_num, players_map

# ── Unified Extractor Router ──────────────────────────────────────────────────
def extract_stats(stats_html: str, roster_html: str, school_meta: Dict[str, Any], year: str) -> Dict[str, Any]:
    roster_by_num, roster_list = parse_roster_page(roster_html)
    logger.info("  [%s] Parsed %d roster records (%d mapped with jersey #s)", school_meta["name"], len(roster_list), len(roster_by_num))

    soup = BeautifulSoup(stats_html, "html.parser")
    res = None

    if school_meta.get("type") == "statbroadcast":
        res = parse_statbroadcast(stats_html, roster_by_num, roster_list)

    if not res or not (res[0] or res[1]):
        res = try_extract_nuxt(soup, roster_by_num, roster_list)

    if not res or not (res[0] or res[1]):
        logger.info("  [%s] Falling back to Sidearm HTML table parser...", school_meta["name"])
        res = try_extract_sidearm_html(soup, roster_by_num, roster_list)

    hitting_list, pitching_list, hitting_by_num, pitching_by_num, players_map = res

    hitting_list.sort(key=lambda x: x.pop("_sort", 999))
    pitching_list.sort(key=lambda x: x.pop("_sort", 999))
    players_sorted = [players_map[k] for k in sorted(players_map.keys())]

    return {
        "team_id": school_meta["id"],
        "team": school_meta["name"],
        "sport": "baseball",
        "season": year,
        "source": school_meta["stats_url"].format(year=year),
        "source_roster": school_meta["roster_url"],
        "status": "ok" if (hitting_list or pitching_list) else "empty",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "players": players_sorted,
        "hitting": hitting_list,
        "pitching": pitching_list,
        "batters": hitting_list,
        "pitchers": pitching_list,
        "hitting_by_number": hitting_by_num,
        "pitching_by_number": pitching_by_num,
        "batters_by_number": hitting_by_num,
        "pitchers_by_number": pitching_by_num
    }

# ── Fetch Helpers ──────────────────────────────────────────────────────────────
def fetch_html_static(url: str) -> str:
    req = urllib.request.Request(url, headers=FETCH_HEADERS)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", errors="replace")

async def fetch_page_html(context: BrowserContext, url: str) -> Optional[str]:
    page: Optional[Page] = None
    try:
        page = await context.new_page()
        logger.info("Navigating to %s", url)
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_selector("script#__NUXT_DATA__, table, .sidearm-roster-player", state="attached", timeout=12000)
        except Exception:
            await page.wait_for_timeout(2000)
        return await page.content()
    except Exception as e:
        logger.error("Failed to fetch %s: %s", url, e)
        return None
    finally:
        if page:
            await page.close()

# ── Main Async Runner ──────────────────────────────────────────────────────────
async def run(args: argparse.Namespace) -> None:
    year = str(args.year or "2026")
    docs_dir = args.docs_dir
    os.makedirs(docs_dir, exist_ok=True)

    schools_to_scrape = TEAMS
    if args.school:
        schools_to_scrape = [t for t in TEAMS if t["id"] == args.school.lower()]

    logger.info("=== Summit League Baseball Scraper [%d School(s), Season: %s] ===", len(schools_to_scrape), year)
    all_teams_data = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=FETCH_HEADERS["User-Agent"], viewport={"width": 1280, "height": 800})

        for school in schools_to_scrape:
            stats_url = school["stats_url"].format(year=year)
            roster_url = school["roster_url"]

            logger.info("Processing Baseball: %s...", school["name"])
            try:
                # 1. Fetch Stats
                if school.get("type") == "statbroadcast":
                    try:
                        stats_html = await asyncio.to_thread(fetch_html_static, stats_url)
                    except Exception:
                        stats_html = await fetch_page_html(context, school["fallback_stats_url"].format(year=year))
                else:
                    stats_html = await fetch_page_html(context, stats_url)

                # 2. Fetch Roster
                roster_html = await fetch_page_html(context, roster_url)

                # 3. Extract & Save
                stats_payload = extract_stats(stats_html or "", roster_html or "", school, year)

                out_path = os.path.join(docs_dir, school["out_file"])
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(stats_payload, f, indent=2)

                if "alias_file" in school:
                    alias_path = os.path.join(docs_dir, school["alias_file"])
                    with open(alias_path, "w", encoding="utf-8") as f:
                        json.dump(stats_payload, f, indent=2)

                all_teams_data[school["id"]] = stats_payload
                logger.info("  ✓ Saved %d batters and %d pitchers -> %s", len(stats_payload["hitting"]), len(stats_payload["pitching"]), school["out_file"])
            except Exception as e:
                logger.exception("  ✗ Error processing %s: %s", school["name"], e)

        await browser.close()

    # Save Master File
    if len(all_teams_data) > 1:
        master_file = os.path.join(docs_dir, "all_teams_stats.json")
        with open(master_file, "w", encoding="utf-8") as f:
            json.dump({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "season": year,
                "teams": all_teams_data
            }, f, indent=2)
        logger.info("★ Master file saved: %s (%d teams)", master_file, len(all_teams_data))

def main() -> None:
    parser = argparse.ArgumentParser(description="Summit League Baseball Stats Scraper")
    parser.add_argument("--year", default="2026", help="Season year")
    parser.add_argument("--docs-dir", default=DEFAULT_DOCS_DIR, help="Output destination folder")
    parser.add_argument("--school", default=None, help="Scrape specific school ID")
    asyncio.run(run(parser.parse_args()))

if __name__ == "__main__":
    main()
