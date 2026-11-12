#!/usr/bin/env python3
"""
scrape_baseball.py
Unified Summit League Baseball Stats & Roster Scraper (6 Conference Teams + Optional Non-Conf Opponent).

Teams:
  1. St. Thomas Tommies (stthomas.json / baseball_stats.json)
  2. South Dakota State Jackrabbits (sdstate.json)
  3. Oral Roberts Golden Eagles (oru.json)
  4. Omaha Mavericks (omaha.json)
  5. Northern Colorado Bears (uncbears.json)
  6. North Dakota State Bison (ndsu.json)
  7. Optional Non-Conf Opponent (non_conf.json) driven by non_conf_config.json

Features:
  - Supports Sidearm New Style (Nuxt 3 __NUXT_DATA__ JSON payloads) and Old Style (Classic HTML tables).
  - StatBroadcast HTML parsing fallback for legacy/custom stat sites (NDSU).
  - Robust print-roster parser extracting Player Name, Jersey Number, Position (standardized G/F/W/P/C/INF/OF/etc.), and Academic Class (FR/SO/JR/SR/GR/R-*).
  - Standardized JSON layout optimized for XPression DataLinq integration (number first, consistent keys across hitting, pitching, and master player roster).
  - Async Playwright execution with concurrency limits.
  - Safe updates: empty or failed scrapes never overwrite good existing data.

Usage:
  python scrape_baseball.py
  python scrape_baseball.py --docs-dir docs --year 2026
  python scrape_baseball.py --school stthomas
"""

import argparse
import asyncio
import difflib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, BrowserContext, Page

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

CONCURRENCY_LIMIT = 4
DEFAULT_DOCS_DIR = "docs"
MIN_ROSTER_ENTRIES = 10
NON_CONF_CONFIG_PATH = "non_conf_config.json"
NON_CONF_SLUG = "non_conf"

# ── Summit League Baseball Configuration ───────────────────────────────────────
SCHOOLS = [
    {
        "id": "stthomas",
        "name": "St. Thomas Tommies",
        "shortName": "St. Thomas",
        "stats_url": "https://tommiesports.com/sports/baseball/stats/{year}",
        "roster_url": "https://tommiesports.com/sports/baseball/roster/print",
        "out_file": "stthomas.json",
        "alias_file": "baseball_stats.json"
    },
    {
        "id": "sdstate",
        "name": "South Dakota State Jackrabbits",
        "shortName": "South Dakota State",
        "stats_url": "https://gojacks.com/sports/baseball/stats/{year}",
        "roster_url": "https://gojacks.com/sports/baseball/roster/print",
        "out_file": "sdstate.json"
    },
    {
        "id": "oru",
        "name": "Oral Roberts Golden Eagles",
        "shortName": "Oral Roberts",
        "stats_url": "https://oruathletics.com/sports/baseball/stats/{year}",
        "roster_url": "https://oruathletics.com/sports/baseball/roster?print=true",
        "out_file": "oru.json"
    },
    {
        "id": "omaha",
        "name": "Omaha Mavericks",
        "shortName": "Omaha",
        "stats_url": "https://omahamavs.com/sports/baseball/stats/{year}",
        "roster_url": "https://omahamavs.com/sports/baseball/roster/print",
        "out_file": "omaha.json"
    },
    {
        "id": "uncbears",
        "name": "Northern Colorado Bears",
        "shortName": "Northern Colorado",
        "stats_url": "https://uncbears.com/sports/baseball/stats/{year}",
        "roster_url": "https://uncbears.com/sports/baseball/roster/print",
        "out_file": "uncbears.json"
    },
    {
        "id": "ndsu",
        "name": "North Dakota State Bison",
        "shortName": "North Dakota State",
        "stats_url": "https://stats.gobison.com/custompages/bsb/{year}/teamcume.htm",
        "sidearm_stats_url": "https://gobison.com/sports/baseball/stats/{year}",
        "roster_url": "https://gobison.com/sports/baseball/roster/print",
        "out_file": "ndsu.json"
    }
]

DISCARD_EXACT_TOKENS = {
    "avg", "ab", "r", "h", "2b", "3b", "hr", "rbi", "tb", "slg%", "bb", "hbp",
    "so", "gdp", "ob%", "sf", "sh", "sb-att", "era", "whip", "w-l", "app", "gs",
    "cg", "sho", "sv", "ip", "er", "total", "totals", "opponent", "opponents",
    "player", "no.", "name", "pos.", "ht.", "wt.", "cl.", "hometown", "high school",
    "view bio", "bio", "team", "tm", "#tm", "unassigned"
}

STAFF_KEYWORDS = [
    "coach", "trainer", "director", "coordinator", "manager", "athletic",
    "operations", "strength", "conditioning", "physician", "assistant",
    "communications", "compliance", "academic", "marketing"
]

_CLASS_MAP = {
    "FR": "FR", "FRESHMAN": "FR", "FY": "FR", "FIRST": "FR", "FIRSTYEAR": "FR", "1ST": "FR", "1": "FR",
    "SO": "SO", "SOPHOMORE": "SO", "SECOND": "SO", "SECONDYEAR": "SO", "2ND": "SO", "2": "SO",
    "JR": "JR", "JUNIOR": "JR", "THIRD": "JR", "THIRDYEAR": "JR", "3RD": "JR", "3": "JR",
    "SR": "SR", "SENIOR": "SR", "FOURTH": "SR", "FOURTHYEAR": "SR", "4TH": "SR", "4": "SR",
    "GR": "GR", "GRAD": "GR", "GRADUATE": "GR", "GRADUATESTUDENT": "GR", "GS": "GR", "5TH": "GR"
}


# ── Cleaners & Normalization ──────────────────────────────────────────────────

def clean_val(v: Any, default: str = "0") -> str:
    if v is None:
        return default
    s = str(v).strip().replace("\xa0", "").replace("&nbsp;", "")
    if s in ("-1", "", "None", "--", "---", "-.---", "-"):
        return default
    try:
        f = float(s)
        return str(int(round(f)))
    except Exception:
        m = re.match(r"^(\d+)", s)
        return m.group(1) if m else default


def clean_avg(v: Any, default: str = ".000") -> str:
    if v is None:
        return default
    s = str(v).strip().replace("\xa0", "").replace("&nbsp;", "").replace("%", "")
    if s in ("-1", "", "None", "-", "--", "---", "-.---", "N/A", "na"):
        return default
    try:
        num = float(s)
        if num == 0.0:
            return default
        if default == ".000":
            if num > 1.0:
                num = num / 100.0
            if num >= 1.0:
                return "1.000"
            formatted = f"{num:.3f}"
            return formatted.lstrip("0") if formatted.startswith("0.") else formatted
        return f"{num:.2f}"
    except Exception:
        return default


def parse_float(v: Any) -> float:
    try:
        return float(str(v).replace("-", "0").replace("\xa0", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def parse_ip(ip_str: Any) -> float:
    """Handles baseball IP format: '42.1' = 42 and 1/3 innings."""
    try:
        parts = str(ip_str).strip().split(".")
        full = int(parts[0])
        outs = int(parts[1]) if len(parts) > 1 else 0
        return full + (outs / 3.0)
    except Exception:
        return 0.0


def compute_ops(obp_str: str, slg_str: str) -> str:
    try:
        total = parse_float(obp_str) + parse_float(slg_str)
        if total == 0:
            return ".000"
        formatted = f"{total:.3f}"
        return formatted.lstrip("0") if formatted.startswith("0.") else formatted
    except Exception:
        return ".000"


def compute_whip(bb: str, h: str, ip_str: str) -> str:
    try:
        ip_dec = parse_ip(ip_str)
        if ip_dec <= 0:
            return "-"
        return f"{(parse_float(bb) + parse_float(h)) / ip_dec:.2f}"
    except Exception:
        return "-"


def format_player_name(raw_name: str) -> str:
    """Normalizes 'Lastname, Firstname' or 'Firstname Lastname' into clean 'Firstname Lastname'."""
    if not raw_name:
        return ""
    name = str(raw_name).strip()
    name = name.replace("View Bio", "").replace("Image", "").strip()
    name = re.sub(r"['\"]+", "", name).strip()
    name = re.sub(r"\([^)]*\)", "", name)
    name = re.sub(r"#\d{1,2}\b", " ", name)
    name = re.sub(r"^\d+\s+", "", name).strip()

    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        p1 = re.sub(r"\b\d{1,2}\b", "", parts[0]).strip()
        p2 = re.sub(r"\b\d{1,2}\b", "", parts[1]).strip() if len(parts) > 1 else ""
        if p1 and p2:
            name = f"{p2} {p1}"
        else:
            name = p2 or p1

    name = re.sub(r"\b\d{1,2}\b", "", name)
    name = " ".join(name.split()).strip()
    return name


def normalize_position(pos_str: str) -> str:
    if not pos_str:
        return ""
    p = pos_str.upper().strip().rstrip(".")
    if re.fullmatch(r"(RHP|LHP|PITCHER|P|RELIEF PITCHER|SP|RP)", p):
        return p if p in ("RHP", "LHP") else "P"
    if re.fullmatch(r"(C|CATCHER)", p):
        return "C"
    if re.fullmatch(r"(INF|INFIELDER|1B|2B|3B|SS|FIRST BASE|SECOND BASE|THIRD BASE|SHORTSTOP)", p):
        return p if p in ("1B", "2B", "3B", "SS", "INF") else "INF"
    if re.fullmatch(r"(OF|OUTFIELDER|LF|CF|RF|LEFT FIELD|CENTER FIELD|RIGHT FIELD)", p):
        return p if p in ("LF", "CF", "RF", "OF") else "OF"
    if re.fullmatch(r"(DH|UTIL|UTL|DESIGNATED HITTER|UTILITY)", p):
        return "DH" if "DH" in p else "UTIL"
    return p[:6]


def normalize_year(yr_str: str) -> str:
    if not yr_str:
        return ""
    y = re.sub(r"[^A-Z0-9]", "", str(yr_str).upper())
    if not y:
        return ""
    if y in _CLASS_MAP:
        return _CLASS_MAP[y]
    for prefix in ("REDSHIRT", "RS", "R"):
        if y.startswith(prefix):
            rem = y[len(prefix):]
            if rem in _CLASS_MAP:
                base = _CLASS_MAP[rem]
                return f"R-{base}" if base != "GR" else "GR"
    return ""


def clean_number(raw_num: Any) -> str:
    if raw_num is None:
        return ""
    text = str(raw_num).strip().replace("#", "")
    if text.upper() in ("TM", "TEAM", "TOT", "TOTAL"):
        return ""
    m = re.match(r"^(\d{1,2})$", text)
    if not m:
        return ""
    return str(int(m.group(1)))


def is_valid_player_name(name: str) -> bool:
    if not name or len(name.strip()) < 2:
        return False
    raw_low = name.strip().lower()
    condensed = re.sub(r"[\s\-_#]+", "", raw_low)
    if raw_low in DISCARD_EXACT_TOKENS or condensed in DISCARD_EXACT_TOKENS:
        return False
    if any(tok in condensed for tok in ["teamtm", "teamteam", "tmteam", "total", "opponent", "opponents"]):
        return False
    if any(staff in raw_low for staff in STAFF_KEYWORDS):
        return False
    return True


# ── Non-Conf Config Helpers ───────────────────────────────────────────────────

def load_non_conf_config() -> Dict[str, str]:
    cfg = {"name": "", "stats_url": "", "roster_url": ""}
    try:
        with open(NON_CONF_CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            # Supports {"baseball": {...}} or direct {...}
            entry = raw.get("baseball") if "baseball" in raw else raw
            if isinstance(entry, dict):
                for k in ("name", "stats_url", "roster_url"):
                    cfg[k] = str(entry.get(k, "") or "").strip()
    except FileNotFoundError:
        logger.info(f"{NON_CONF_CONFIG_PATH} not found - Non-Conf baseball slot is empty")
    except Exception as e:
        logger.error(f"Could not read {NON_CONF_CONFIG_PATH}: {e}")
    return cfg


def derive_roster_candidates(stats_url: str) -> List[str]:
    base = stats_url.strip().split("#")[0].split("?")[0].rstrip("/")
    m = re.match(r"^(.*?)/stats(?:/(\d{4}))?$", base)
    if not m:
        return []
    root, season = m.group(1), m.group(2)
    cands: List[str] = []
    if season:
        cands += [f"{root}/roster/print/{season}", f"{root}/roster/{season}?print=true"]
    cands += [f"{root}/roster/print", f"{root}/roster?print=true"]
    if season:
        cands.append(f"{root}/roster/{season}")
    cands.append(f"{root}/roster")
    seen, unique = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def detect_team_name(html: Optional[str], url: str) -> str:
    host = urlparse(url).netloc.lower().replace("www.", "")
    if html:
        try:
            soup = BeautifulSoup(html, "html.parser")
            og = soup.find("meta", attrs={"property": "og:site_name"})
            if og and og.get("content", "").strip():
                return og["content"].strip()
            if soup.title and soup.title.string:
                parts = re.split(r"\s+[|\-–—]\s+", soup.title.string.strip())
                good = [p for p in parts if p.strip() and not re.search(r"stat|baseball|roster|\d{4}", p, re.I)]
                if good:
                    return good[-1].strip()
        except Exception:
            pass
    return host or "Non-Conf Opponent"


# ── Roster Parser ─────────────────────────────────────────────────────────────

def parse_print_roster(html: str) -> Dict[str, Dict[str, str]]:
    roster_lookup: Dict[str, Dict[str, str]] = {}
    if not html:
        return roster_lookup

    # 1. Try Nuxt Data Script first
    tag = re.search(r'<script[^>]+id=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>', html, re.DOTALL)
    if tag:
        try:
            pool = json.loads(tag.group(1))
            if isinstance(pool, list):
                def resolve(v):
                    if isinstance(v, int) and 0 <= v < len(pool):
                        t = pool[v]
                        if isinstance(t, (str, int, float)):
                            return str(t)
                    return str(v) if v is not None else ""

                for item in pool:
                    if isinstance(item, dict):
                        raw_name = item.get("name") or item.get("playerName") or item.get("fullName")
                        if not raw_name:
                            continue
                        c_name = format_player_name(resolve(raw_name))
                        if not is_valid_player_name(c_name):
                            continue
                        c_num = clean_number(resolve(item.get("uniform") or item.get("jersey") or item.get("jerseyNumber")))
                        c_pos = normalize_position(resolve(item.get("position") or item.get("pos") or item.get("positionShort")))
                        c_yr = normalize_year(resolve(item.get("academicYear") or item.get("year") or item.get("class")))
                        info = {"name": c_name, "number": c_num, "position": c_pos, "year": c_yr}
                        roster_lookup[c_name.lower()] = info
                        if c_num:
                            roster_lookup[f"#{c_num}"] = info
        except Exception:
            pass

    if len(roster_lookup) >= 8:
        return roster_lookup

    # 2. BeautifulSoup HTML Roster Parsing
    soup = BeautifulSoup(html, "html.parser")
    for staff in soup.find_all(class_=re.compile(r"staff|coaches|coach", re.I)):
        staff.decompose()

    for row in soup.find_all("tr"):
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) < 3:
            continue
        num, pos, yr, name_cand = "", "", "", ""
        for col in cells:
            if not num and re.match(r"^\d{1,2}$", col):
                num = clean_number(col)
                continue
            if not pos and normalize_position(col):
                pos = normalize_position(col)
                continue
            if not yr and normalize_year(col):
                yr = normalize_year(col)
                continue
            c_clean = format_player_name(col)
            if not name_cand and is_valid_player_name(c_clean):
                name_cand = c_clean

        if name_cand and is_valid_player_name(name_cand):
            info = {"name": name_cand, "number": num, "position": pos, "year": yr}
            roster_lookup[name_cand.lower()] = info
            if num:
                roster_lookup[f"#{num}"] = info

    return roster_lookup


def find_roster_entry(name: str, number: str, roster_lookup: Dict[str, Dict[str, str]]) -> Optional[Dict[str, str]]:
    if not roster_lookup:
        return None
    raw_low = name.lower().strip()
    if raw_low in roster_lookup:
        return roster_lookup[raw_low]
    if number and f"#{number}" in roster_lookup:
        return roster_lookup[f"#{number}"]

    parts = raw_low.split()
    if parts:
        last = parts[-1]
        for k, v in roster_lookup.items():
            if k.startswith("#"):
                continue
            if k.split() and k.split()[-1] == last:
                if not number or v.get("number") == number:
                    return v

    names = [k for k in roster_lookup.keys() if not k.startswith("#")]
    close = difflib.get_close_matches(raw_low, names, n=1, cutoff=0.75)
    if close:
        return roster_lookup[close[0]]
    return None


# ── Sidearm Nuxt 3 & HTML Extractors ──────────────────────────────────────────

def extract_sidearm_nuxt(html: str) -> Optional[Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]:
    tag = re.search(r'<script[^>]+id=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>', html, re.DOTALL)
    if not tag:
        return None
    try:
        pool = json.loads(tag.group(1))
        if not isinstance(pool, list):
            return None

        def resolve(v):
            if isinstance(v, int) and 0 <= v < len(pool):
                t = pool[v]
                if isinstance(t, (str, int, float)):
                    return str(t)
            return str(v) if v is not None else ""

        def find_stat(item: dict, keys: List[str], default: str = "0") -> str:
            for k in keys:
                v = item.get(k)
                if v is not None:
                    res = resolve(v).strip()
                    if res and res != "None" and res.lower() not in DISCARD_EXACT_TOKENS:
                        return res
            return default

        batting_list = []
        pitching_list = []

        for item in pool:
            if not isinstance(item, dict) or item.get("isAFooterStat") is True:
                continue

            raw_name = item.get("playerName") or item.get("name") or item.get("fullName") or item.get("displayName")
            if not raw_name:
                continue

            c_name = format_player_name(resolve(raw_name))
            if not is_valid_player_name(c_name):
                continue

            c_num = clean_number(resolve(item.get("playerUniform") or item.get("jersey") or item.get("uniform")))
            c_pos = normalize_position(resolve(item.get("position") or item.get("pos")))
            c_yr = normalize_year(resolve(item.get("academicYear") or item.get("year") or item.get("class")))

            # Hitting extraction
            ab = find_stat(item, ["atBats", "ab", "at_bats"])
            h = find_stat(item, ["hits", "h"])
            r = find_stat(item, ["runs", "r"])
            d2 = find_stat(item, ["doubles", "2b", "two_base_hits"])
            d3 = find_stat(item, ["triples", "3b", "three_base_hits"])
            hr = find_stat(item, ["homeRuns", "hr", "home_runs"])
            rbi = find_stat(item, ["runsBattedIn", "rbi", "runs_batted_in"])
            avg = clean_avg(find_stat(item, ["battingAverage", "avg", "batting_average"], ".000"))
            slg = clean_avg(find_stat(item, ["sluggingPercentage", "slg", "slugging_pct"], ".000"))
            obp = clean_avg(find_stat(item, ["onBasePercentage", "obp", "on_base_pct"], ".000"))
            ops = clean_avg(find_stat(item, ["ops", "onBasePlusSlugging"], compute_ops(obp, slg)))

            if parse_float(ab) > 0 or parse_float(h) > 0 or avg != ".000":
                batting_list.append({
                    "number": c_num,
                    "player": c_name,
                    "position": c_pos,
                    "year": c_yr,
                    "AVG": avg,
                    "R": clean_val(r),
                    "H": clean_val(h),
                    "2B": clean_val(d2),
                    "3B": clean_val(d3),
                    "HR": clean_val(hr),
                    "RBI": clean_val(rbi),
                    "OPS": ops,
                    "SLG%": slg
                })

            # Pitching extraction
            ip = find_stat(item, ["inningsPitched", "ip", "innings_pitched"], "0.0")
            era = clean_avg(find_stat(item, ["earnedRunAverage", "era", "earned_run_avg"], "-"), "-")
            whip = clean_avg(find_stat(item, ["whip"], "-"), "-")
            wins = clean_val(find_stat(item, ["wins", "w"]))
            loss = clean_val(find_stat(item, ["losses", "l"]))
            so = clean_val(find_stat(item, ["strikeoutsPitching", "so", "strikeouts", "pitchingStrikeouts"]))

            if parse_float(ip) > 0 or (era != "-" and era != ".000"):
                pitching_list.append({
                    "number": c_num,
                    "player": c_name,
                    "position": c_pos or "P",
                    "year": c_yr,
                    "ERA": era,
                    "WHIP": whip,
                    "W-L": f"{wins}-{loss}",
                    "IP": ip,
                    "SO": so
                })

        if batting_list or pitching_list:
            return batting_list, pitching_list
    except Exception:
        pass
    return None


def extract_sidearm_html_tables(soup: BeautifulSoup) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    batting_list = []
    pitching_list = []

    def get_cell(tr, label, default="0"):
        td = tr.find(lambda tag: tag.name in ["td", "th"] and tag.get("data-label", "").lower() == label.lower())
        if td:
            return td.get_text(" ", strip=True)
        return default

    # Batting
    batting_section = soup.find(id=re.compile(r"batting", re.I)) or soup.find("table", class_=re.compile(r"batting", re.I))
    batting_table = batting_section.find("table") if (batting_section and batting_section.name != "table") else batting_section

    if not batting_table:
        for t in soup.find_all("table"):
            cap = t.find("caption")
            if cap and "batting" in cap.get_text().lower():
                batting_table = t
                break

    if batting_table:
        tbody = batting_table.find("tbody") or batting_table
        for tr in tbody.find_all("tr"):
            num_el = tr.find(class_=re.compile(r"jersey|number", re.I)) or tr.find("td")
            num = clean_number(num_el.get_text(strip=True)) if num_el else ""
            name_a = tr.find("a", href=re.compile(r"roster", re.I)) or tr.find("a")
            raw_name = name_a.get_text(strip=True) if name_a else (tr.find_all("td")[1].get_text(strip=True) if len(tr.find_all("td")) > 1 else "")
            name = format_player_name(raw_name)
            if not is_valid_player_name(name):
                continue

            avg = clean_avg(get_cell(tr, "AVG", ".000"))
            ab = clean_val(get_cell(tr, "AB", "0"))
            r = clean_val(get_cell(tr, "R", "0"))
            h = clean_val(get_cell(tr, "H", "0"))
            d2 = clean_val(get_cell(tr, "2B", "0"))
            d3 = clean_val(get_cell(tr, "3B", "0"))
            hr = clean_val(get_cell(tr, "HR", "0"))
            rbi = clean_val(get_cell(tr, "RBI", "0"))
            ops = clean_avg(get_cell(tr, "OPS", ".000"))
            slg = clean_avg(get_cell(tr, "SLG%", ".000"))

            if parse_float(ab) > 0 or parse_float(h) > 0 or avg != ".000":
                batting_list.append({
                    "number": num,
                    "player": name,
                    "position": "",
                    "year": "",
                    "AVG": avg,
                    "R": r,
                    "H": h,
                    "2B": d2,
                    "3B": d3,
                    "HR": hr,
                    "RBI": rbi,
                    "OPS": ops,
                    "SLG%": slg
                })

    # Pitching
    pitching_section = soup.find(id=re.compile(r"pitching", re.I)) or soup.find("table", class_=re.compile(r"pitching", re.I))
    pitching_table = pitching_section.find("table") if (pitching_section and pitching_section.name != "table") else pitching_section

    if not pitching_table:
        for t in soup.find_all("table"):
            cap = t.find("caption")
            if cap and "pitching" in cap.get_text().lower():
                pitching_table = t
                break

    if pitching_table:
        tbody = pitching_table.find("tbody") or pitching_table
        for tr in tbody.find_all("tr"):
            num_el = tr.find(class_=re.compile(r"jersey|number", re.I)) or tr.find("td")
            num = clean_number(num_el.get_text(strip=True)) if num_el else ""
            name_a = tr.find("a", href=re.compile(r"roster", re.I)) or tr.find("a")
            raw_name = name_a.get_text(strip=True) if name_a else (tr.find_all("td")[1].get_text(strip=True) if len(tr.find_all("td")) > 1 else "")
            name = format_player_name(raw_name)
            if not is_valid_player_name(name):
                continue

            era = clean_avg(get_cell(tr, "ERA", "-"), "-")
            whip = clean_avg(get_cell(tr, "WHIP", "-"), "-")
            wl = get_cell(tr, "W-L", "0-0").strip()
            ip = get_cell(tr, "IP", "0.0").strip()
            so = clean_val(get_cell(tr, "SO", "0"))

            if parse_float(ip) > 0 or era != "-":
                pitching_list.append({
                    "number": num,
                    "player": name,
                    "position": "P",
                    "year": "",
                    "ERA": era,
                    "WHIP": whip,
                    "W-L": wl,
                    "IP": ip,
                    "SO": so
                })

    return batting_list, pitching_list


def extract_ndsu_statbroadcast(soup: BeautifulSoup) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Parses NDSU StatBroadcast static HTML cumulative stat table if Sidearm is unavailable."""
    batting_list = []
    pitching_list = []

    for table in soup.find_all("table"):
        text = table.get_text(" ", strip=True).lower()
        rows = table.find_all("tr")
        if not rows:
            continue

        # Detect batting table
        if "avg" in text and "ab" in text and "rbi" in text:
            header_cols = [c.get_text(strip=True).lower() for c in rows[0].find_all(["th", "td"])]
            for r in rows[1:]:
                cells = [c.get_text(strip=True) for c in r.find_all(["th", "td"])]
                if len(cells) < 8 or not cells[0] or "total" in cells[0].lower() or "opponent" in cells[0].lower():
                    continue
                name = format_player_name(cells[0])
                if is_valid_player_name(name):
                    batting_list.append({
                        "number": "",
                        "player": name,
                        "position": "",
                        "year": "",
                        "AVG": clean_avg(cells[1] if len(cells) > 1 else ".000"),
                        "R": clean_val(cells[3] if len(cells) > 3 else "0"),
                        "H": clean_val(cells[4] if len(cells) > 4 else "0"),
                        "2B": clean_val(cells[5] if len(cells) > 5 else "0"),
                        "3B": clean_val(cells[6] if len(cells) > 6 else "0"),
                        "HR": clean_val(cells[7] if len(cells) > 7 else "0"),
                        "RBI": clean_val(cells[8] if len(cells) > 8 else "0"),
                        "OPS": ".000",
                        "SLG%": ".000"
                    })

        # Detect pitching table
        elif "era" in text and "w-l" in text and "ip" in text:
            for r in rows[1:]:
                cells = [c.get_text(strip=True) for c in r.find_all(["th", "td"])]
                if len(cells) < 7 or not cells[0] or "total" in cells[0].lower() or "opponent" in cells[0].lower():
                    continue
                name = format_player_name(cells[0])
                if is_valid_player_name(name):
                    pitching_list.append({
                        "number": "",
                        "player": name,
                        "position": "P",
                        "year": "",
                        "ERA": clean_avg(cells[1] if len(cells) > 1 else "-", "-"),
                        "WHIP": "-",
                        "W-L": cells[2] if len(cells) > 2 else "0-0",
                        "IP": cells[6] if len(cells) > 6 else "0.0",
                        "SO": clean_val(cells[11] if len(cells) > 11 else "0")
                    })

    return batting_list, pitching_list


# ── Execution Logic ───────────────────────────────────────────────────────────

async def fetch_page_html(context: BrowserContext, url: str) -> Optional[str]:
    page: Optional[Page] = None
    try:
        page = await context.new_page()
        logger.info(f"Navigating to {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        try:
            await page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        return await page.content()
    except Exception as e:
        logger.error(f"Failed to fetch {url}: {e}")
        return None
    finally:
        if page:
            await page.close()


async def scrape_baseball_team(
    context: BrowserContext,
    team_cfg: Dict[str, Any],
    year: str,
    semaphore: asyncio.Semaphore,
    docs_dir: str
) -> Dict[str, Any]:
    async with semaphore:
        team_id = team_cfg["id"]
        is_non_conf = bool(team_cfg.get("is_non_conf"))
        name = team_cfg.get("name", "")
        stats_url = team_cfg["stats_url"].format(year=year)
        roster_url = team_cfg["roster_url"]

        logger.info(f"Processing Baseball: {name or stats_url}...")

        # Stats & Roster fetched in parallel
        stats_html, roster_html = await asyncio.gather(
            fetch_page_html(context, stats_url),
            fetch_page_html(context, roster_url)
        )

        # Fallback for NDSU sidearm URL if StatBroadcast returns empty
        if not stats_html and "sidearm_stats_url" in team_cfg:
            alt_url = team_cfg["sidearm_stats_url"].format(year=year)
            logger.info(f"Trying alternative stats URL for {name}: {alt_url}")
            stats_html = await fetch_page_html(context, alt_url)

        roster_lookup = parse_print_roster(roster_html) if roster_html else {}
        logger.info(f"  [{name}] Loaded {len(roster_lookup)} roster entries from {roster_url}")

        batting_raw: List[Dict[str, Any]] = []
        pitching_raw: List[Dict[str, Any]] = []

        if stats_html:
            # 1. Try Nuxt JSON
            nuxt_res = extract_sidearm_nuxt(stats_html)
            if nuxt_res and (nuxt_res[0] or nuxt_res[1]):
                batting_raw, pitching_raw = nuxt_res
                logger.info(f"  [{name}] Extracted Nuxt 3 stats: {len(batting_raw)} batters, {len(pitching_raw)} pitchers")
            else:
                # 2. Try HTML table parsing
                soup = BeautifulSoup(stats_html, "html.parser")
                batting_raw, pitching_raw = extract_sidearm_html_tables(soup)
                if not batting_raw and not pitching_raw:
                    # 3. StatBroadcast fallback
                    batting_raw, pitching_raw = extract_ndsu_statbroadcast(soup)
                logger.info(f"  [{name}] Extracted HTML stats: {len(batting_raw)} batters, {len(pitching_raw)} pitchers")

        # Merge with roster metadata
        def enrich_player(p_dict: Dict[str, Any]) -> Dict[str, Any]:
            p_name = p_dict["player"]
            p_num = p_dict["number"]
            r_info = find_roster_entry(p_name, p_num, roster_lookup)
            if r_info:
                p_dict["player"] = r_info.get("name") or p_name
                p_dict["number"] = p_num or r_info.get("number", "")
                p_dict["position"] = r_info.get("position") or p_dict.get("position", "")
                p_dict["year"] = r_info.get("year") or p_dict.get("year", "")
            return p_dict

        batting_final = [enrich_player(b) for b in batting_raw]
        pitching_final = [enrich_player(p) for p in pitching_raw]

        # Build master unified players array (keyed by number/name)
        players_map: Dict[str, Dict[str, Any]] = {}

        for b in batting_final:
            key = f"{b['number']}_{b['player']}".lower()
            players_map[key] = {
                "number": b["number"],
                "player": b["player"],
                "position": b["position"],
                "year": b["year"],
                "batting": {
                    "AVG": b["AVG"], "R": b["R"], "H": b["H"], "2B": b["2B"],
                    "3B": b["3B"], "HR": b["HR"], "RBI": b["RBI"], "OPS": b["OPS"], "SLG%": b["SLG%"]
                }
            }

        for p in pitching_final:
            key = f"{p['number']}_{p['player']}".lower()
            if key in players_map:
                players_map[key]["pitching"] = {
                    "ERA": p["ERA"], "WHIP": p["WHIP"], "W-L": p["W-L"], "IP": p["IP"], "SO": p["SO"]
                }
                if not players_map[key]["position"]:
                    players_map[key]["position"] = p["position"]
            else:
                players_map[key] = {
                    "number": p["number"],
                    "player": p["player"],
                    "position": p["position"],
                    "year": p["year"],
                    "pitching": {
                        "ERA": p["ERA"], "WHIP": p["WHIP"], "W-L": p["W-L"], "IP": p["IP"], "SO": p["SO"]
                    }
                }

        players_sorted = sorted(players_map.values(), key=lambda x: int(x["number"]) if x["number"].isdigit() else 999)

        team_display_name = name or detect_team_name(stats_html, stats_url)
        out_data = {
            "team_id": team_id,
            "team": team_display_name,
            "sport": "baseball",
            "season": year,
            "source": stats_url,
            "source_roster": roster_url,
            "status": "ok" if (batting_final or pitching_final) else "empty",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "players": players_sorted,
            "hitting": batting_final,
            "pitching": pitching_final
        }

        if is_non_conf:
            out_data["is_non_conf"] = True
            out_data["display_name"] = "Non-Conf"

        # Write output file(s)
        out_file = team_cfg["out_file"]
        paths_to_write = [
            out_file,
            os.path.join(docs_dir, out_file)
        ]
        if "alias_file" in team_cfg:
            alias = team_cfg["alias_file"]
            paths_to_write += [alias, os.path.join(docs_dir, alias)]

        for path in paths_to_write:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(out_data, f, indent=2)

        logger.info(f"  ✓ Saved {len(batting_final)} batters and {len(pitching_final)} pitchers -> {out_file}")
        return out_data


async def run_baseball_scraper(year: str, docs_dir: str, target_school: Optional[str] = None):
    os.makedirs(docs_dir, exist_ok=True)
    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

    schools_to_scrape = SCHOOLS
    if target_school:
        schools_to_scrape = [s for s in SCHOOLS if s["id"].lower() == target_school.lower()]
        if not schools_to_scrape:
            logger.error(f"Unknown school ID '{target_school}'. Options: {[s['id'] for s in SCHOOLS]}")
            return

    # Check Non-Conf slot
    non_conf_cfg = load_non_conf_config()
    non_conf_task_cfg = None
    if non_conf_cfg.get("stats_url"):
        non_conf_task_cfg = {
            "id": "non_conf",
            "name": non_conf_cfg.get("name") or "Non-Conf Opponent",
            "shortName": "Non-Conf",
            "stats_url": non_conf_cfg["stats_url"],
            "roster_url": non_conf_cfg.get("roster_url") or non_conf_cfg["stats_url"],
            "out_file": "non_conf.json",
            "is_non_conf": True
        }
    else:
        # Write empty placeholder
        empty_nc = {
            "team_id": "non_conf",
            "team": "Non-Conf",
            "display_name": "Non-Conf",
            "sport": "baseball",
            "season": year,
            "status": "empty",
            "is_non_conf": True,
            "message": "No Non-Conf opponent loaded. Configure non_conf_config.json or pass via workflow.",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "players": [],
            "hitting": [],
            "pitching": []
        }
        for dest in ["non_conf.json", os.path.join(docs_dir, "non_conf.json")]:
            with open(dest, "w", encoding="utf-8") as f:
                json.dump(empty_nc, f, indent=2)

    start_time = time.time()
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US"
        )

        tasks = [
            scrape_baseball_team(context, cfg, year, semaphore, docs_dir)
            for cfg in schools_to_scrape
        ]
        if non_conf_task_cfg and not target_school:
            tasks.append(scrape_baseball_team(context, non_conf_task_cfg, year, semaphore, docs_dir))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_teams_dict = {}
        for r in results:
            if isinstance(r, dict) and "team_id" in r:
                all_teams_dict[r["team_id"]] = r
            elif isinstance(r, Exception):
                logger.error(f"Team scrape error: {r!r}")

        # Write master all_teams_stats.json
        if len(all_teams_dict) > 1:
            master_file = os.path.join(docs_dir, "all_teams_stats.json")
            with open(master_file, "w", encoding="utf-8") as f:
                json.dump({
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "season": year,
                    "sport": "baseball",
                    "teams": all_teams_dict
                }, f, indent=2)
            logger.info(f"★ Saved master file: {master_file} with {len(all_teams_dict)} teams")

        await browser.close()

    elapsed = time.time() - start_time
    logger.info(f"Baseball scraping complete in {elapsed:.1f}s.")


def main():
    parser = argparse.ArgumentParser(description="Summit League Baseball Stats & Roster Scraper")
    parser.add_argument("--year", default="2026", help="Season year (e.g. 2026)")
    parser.add_argument("--docs-dir", default=DEFAULT_DOCS_DIR, help="Output destination folder")
    parser.add_argument("--school", default=None, help="Specific school ID to scrape (stthomas, sdstate, oru, omaha, uncbears, ndsu)")
    args = parser.parse_args()

    asyncio.run(run_baseball_scraper(args.year, args.docs_dir, args.school))


if __name__ == "__main__":
    main()
