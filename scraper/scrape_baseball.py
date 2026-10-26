#!/usr/bin/env python3
"""
scrape_baseball.py
──────────────────
Fetch St. Thomas Tommies Baseball cumulative player stats from:
  https://tommiesports.com/sports/baseball/stats/{year}

The Sidearm Athletics platform renders all tables client-side via JavaScript,
so we use Playwright (headless Chromium) to fully render the page before
parsing — exactly the same approach as the standings scraper.

Output shape (docs/baseball_stats.json):
{
  "team":        "University of St. Thomas Tommies Baseball",
  "season":      "2026",
  "source":      "https://tommiesports.com/sports/baseball/stats/2026",
  "status":      "ok" | "stale" | "error",
  "generated_at": "<ISO-8601 UTC>",

  // Ordered lists — used by the HTML table display
  "hitting":  [ { "number": "12", "player": "John Doe", "GP": "40", "AVG": ".312", ... } ],
  "pitching": [ { "number": "12", "player": "John Doe", "ERA": "2.45", "IP": "38.2", ... } ],

  // Indexed by jersey number — used by the spotlight search
  "hitting_by_number":  { "12": { ... }, "24": { ... } },
  "pitching_by_number": { "12": { ... }, "24": { ... } }
}

Safe by design: if the scrape fails, the last good file is kept and marked
"stale" so the HTML viewer never goes blank.

Usage:
    pip install playwright beautifulsoup4
    playwright install chromium

    python scraper/scrape_baseball.py
    python scraper/scrape_baseball.py --out docs/baseball_stats.json
    python scraper/scrape_baseball.py --year 2025 --out docs/baseball_stats.json
"""

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

# ── Constants ─────────────────────────────────────────────────────────────────

UA       = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
BASE_URL = "https://tommiesports.com/sports/baseball/stats/{year}"

# Hitting columns in display order — keys match Sidearm header text exactly.
# The scraper will try each alias in order and use the first match found.
HITTING_COLS = [
    # ( output_key,  [header aliases to try] )
    ("GP",   ["GP", "G"]),
    ("GS",   ["GS"]),
    ("AB",   ["AB"]),
    ("R",    ["R", "Runs"]),
    ("H",    ["H", "Hits"]),
    ("2B",   ["2B"]),
    ("3B",   ["3B"]),
    ("HR",   ["HR"]),
    ("RBI",  ["RBI"]),
    ("TB",   ["TB"]),
    ("SLG%", ["SLG%", "SLG"]),
    ("BB",   ["BB"]),
    ("HBP",  ["HBP"]),
    ("SO",   ["SO", "K"]),
    ("GDP",  ["GDP"]),
    ("OB%",  ["OB%", "OBP"]),
    ("SF",   ["SF"]),
    ("SH",   ["SH"]),
    ("SB",   ["SB"]),
    ("ATT",  ["ATT", "SBA"]),
    ("AVG",  ["AVG", "BA"]),
]

# Pitching columns — captured dynamically from whatever headers Sidearm provides
# (ERA, IP, W, L, SV, H, R, ER, BB, SO, etc.)
PITCHING_SKIP = {"##", "#", "No", "Player", "Name"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

def norm_header(h: str) -> str:
    """Normalise a header string for fuzzy matching."""
    return re.sub(r"[^a-z0-9]", "", h.lower())

def header_cells(table) -> list[str]:
    row = table.select_one("thead tr") or table.find("tr")
    return [clean(c.get_text()) for c in row.find_all(["th", "td"])] if row else []

def body_rows(table) -> list:
    rows = table.select("tbody tr") or table.find_all("tr")[1:]
    return [r for r in rows if r.find_all(["td", "th"])]

def cell_text(cells: list, idx) -> str:
    if idx is None or idx >= len(cells):
        return ""
    return clean(cells[idx].get_text())

def find_col(hdr_map: dict, *aliases) -> int | None:
    """Return column index for the first matching alias, or None."""
    for alias in aliases:
        idx = hdr_map.get(norm_header(alias))
        if idx is not None:
            return idx
    return None

def jersey_clean(raw: str) -> str:
    """Strip non-numeric chars; return empty string if nothing left."""
    return re.sub(r"[^0-9]", "", raw)


# ── Table Classifiers ─────────────────────────────────────────────────────────

def is_hitting_table(headers: list[str]) -> bool:
    normed = [norm_header(h) for h in headers]
    has_avg = any("avg" in h or "ba" == h for h in normed)
    has_ab  = any(h == "ab" for h in normed)
    has_era = any("era" in h for h in normed)
    has_ip  = any(h == "ip" for h in normed)
    return (has_avg or has_ab) and not (has_era or has_ip)

def is_pitching_table(headers: list[str]) -> bool:
    normed = [norm_header(h) for h in headers]
    return any("era" in h for h in normed) or any(h == "ip" for h in normed)

def has_player_col(headers: list[str]) -> bool:
    normed = [norm_header(h) for h in headers]
    return any("player" in h or "name" in h for h in normed)


# ── Hitting Parser ────────────────────────────────────────────────────────────

def parse_hitting_table(table) -> list[dict]:
    headers  = header_cells(table)
    hdr_map  = {norm_header(h): i for i, h in enumerate(headers)}

    num_idx    = find_col(hdr_map, "##", "#", "No", "Num")
    player_idx = find_col(hdr_map, "Player", "Name")

    if player_idx is None:
        return []

    # Build column index map for each desired stat
    col_indices = {}
    for (out_key, aliases) in HITTING_COLS:
        col_indices[out_key] = find_col(hdr_map, *aliases)

    rows = []
    for tr in body_rows(table):
        cells = tr.find_all(["td", "th"])

        raw_player = cell_text(cells, player_idx)
        if not raw_player or raw_player.lower() in ("player", "name", "totals", "team", ""):
            continue

        # Strip asterisk footnote marker from name
        player_name = raw_player.replace("*", "").strip()

        raw_jersey = cell_text(cells, num_idx) if num_idx is not None else ""
        jersey     = jersey_clean(raw_jersey)

        row = {
            "number": jersey or "—",
            "player": player_name,
        }

        for (out_key, _) in HITTING_COLS:
            idx = col_indices.get(out_key)
            val = cell_text(cells, idx) if idx is not None else None
            if val is not None:
                row[out_key] = val if val not in ("", "-", "—") else "—"

        rows.append(row)

    return rows


# ── Pitching Parser ───────────────────────────────────────────────────────────

def parse_pitching_table(table) -> list[dict]:
    headers  = header_cells(table)
    hdr_map  = {norm_header(h): i for i, h in enumerate(headers)}

    num_idx    = find_col(hdr_map, "##", "#", "No", "Num")
    player_idx = find_col(hdr_map, "Player", "Name")

    if player_idx is None:
        return []

    # All columns except number and player are captured dynamically
    stat_cols = [
        (i, h) for i, h in enumerate(headers)
        if i != num_idx and i != player_idx and h not in PITCHING_SKIP and h
    ]

    rows = []
    for tr in body_rows(table):
        cells = tr.find_all(["td", "th"])

        raw_player = cell_text(cells, player_idx)
        if not raw_player or raw_player.lower() in ("player", "name", "totals", "team", ""):
            continue

        player_name = raw_player.replace("*", "").strip()
        raw_jersey  = cell_text(cells, num_idx) if num_idx is not None else ""
        jersey      = jersey_clean(raw_jersey)

        row = {
            "number": jersey or "—",
            "player": player_name,
        }

        for (col_i, col_hdr) in stat_cols:
            val = cell_text(cells, col_i)
            row[col_hdr] = val if val not in ("", "-", "—") else "—"

        rows.append(row)

    return rows


# ── Full Page Parser ──────────────────────────────────────────────────────────

def parse_stats_page(html: str) -> dict:
    """
    Parse the fully-rendered Sidearm stats page.
    Returns { hitting: [...], pitching: [...] }
    """
    soup = BeautifulSoup(html, "html.parser")

    hitting_rows  = []
    pitching_rows = []

    for table in soup.find_all("table"):
        headers = header_cells(table)
        if not headers or not has_player_col(headers):
            continue

        if is_pitching_table(headers):
            pitching_rows.extend(parse_pitching_table(table))
        elif is_hitting_table(headers):
            hitting_rows.extend(parse_hitting_table(table))

    return {
        "hitting":  hitting_rows,
        "pitching": pitching_rows,
    }


# ── Index Builder ─────────────────────────────────────────────────────────────

def index_by_number(rows: list[dict]) -> dict:
    """
    Build a dict keyed by jersey number for O(1) spotlight lookups.
    If two players share a number (rare), the later one wins — adjust if needed.
    Falls back to player name as key if jersey is missing.
    """
    indexed = {}
    for r in rows:
        key = r.get("number", "—")
        if not key or key == "—":
            key = r.get("player", "unknown")
        indexed[key] = r
    return indexed


# ── Playwright Fetch ──────────────────────────────────────────────────────────

def fetch_and_parse(url: str) -> dict:
    from playwright.sync_api import sync_playwright

    print(f"  → Fetching: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA)
        page    = context.new_page()

        # Block media/fonts to speed up; keep JS so Sidearm tables render
        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "media", "font")
            else route.continue_(),
        )

        page.goto(url, wait_until="domcontentloaded", timeout=45_000)

        # Wait for at least one stat row to appear in the DOM
        try:
            page.wait_for_selector("table tbody tr", timeout=20_000)
            print("  ✓ Table rows detected.")
        except Exception:
            print("  ⚠ Timed out waiting for table rows — parsing whatever loaded.", file=sys.stderr)

        html = page.content()
        browser.close()

    return parse_stats_page(html)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Scrape St. Thomas baseball stats.")
    ap.add_argument("--out",  default="docs/baseball_stats.json",
                    help="Output path for the JSON file (default: docs/baseball_stats.json)")
    ap.add_argument("--year", default="2026",
                    help="Season year to scrape (default: 2026)")
    args = ap.parse_args()

    out_path = Path(args.out)
    url      = BASE_URL.format(year=args.year)

    # ── Load previous data for stale fallback ─────────────────────────────────
    old_data: dict = {}
    if out_path.exists():
        try:
            old_data = json.loads(out_path.read_text(encoding="utf-8"))
            print(f"  ↩ Loaded previous data from {out_path} (stale fallback ready).")
        except Exception:
            pass

    # ── Scrape ────────────────────────────────────────────────────────────────
    try:
        parsed = fetch_and_parse(url)

        hitting_count  = len(parsed["hitting"])
        pitching_count = len(parsed["pitching"])

        if hitting_count == 0 and pitching_count == 0:
            raise ValueError(
                "No player rows found — the page may not have rendered correctly. "
                "Try running again or check the URL."
            )

        payload = {
            "team":               "University of St. Thomas Tommies Baseball",
            "season":             args.year,
            "source":             url,
            "status":             "ok",
            "generated_at":       now_iso(),
            # Ordered lists for the full-roster table display
            "hitting":            parsed["hitting"],
            "pitching":           parsed["pitching"],
            # Indexed by jersey number for the spotlight search card
            "hitting_by_number":  index_by_number(parsed["hitting"]),
            "pitching_by_number": index_by_number(parsed["pitching"]),
        }

        print(f"  ✓ Hitting:  {hitting_count} players parsed.")
        print(f"  ✓ Pitching: {pitching_count} players parsed.")

    except Exception as exc:
        print(f"  ✗ Scrape failed: {exc}", file=sys.stderr)

        if old_data.get("hitting"):
            print("  ↩ Keeping previous data as stale.")
            payload = {
                **old_data,
                "status":       "stale",
                "error":        str(exc)[:400],
                "generated_at": now_iso(),
            }
        else:
            payload = {
                "team":               "University of St. Thomas Tommies Baseball",
                "season":             args.year,
                "source":             url,
                "status":             "error",
                "error":              str(exc)[:400],
                "generated_at":       now_iso(),
                "hitting":            [],
                "pitching":           [],
                "hitting_by_number":  {},
                "pitching_by_number": {},
            }

    # ── Write output ──────────────────────────────────────────────────────────
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\n✓ Written to {out_path}")


if __name__ == "__main__":
    main()
