#!/usr/bin/env python3
"""
scrape_baseball.py
Scrapes Tommie Baseball hitting AND pitching stats by downloading the
official cumulative stats PDF directly from the Sidearm S3 bucket.

PDF URL pattern:
  https://s3.us-east-2.amazonaws.com/sidearm.nextgen.sites/tommiesports.com/stats/baseball/{year}/pdf/cume.pdf

This completely bypasses the JS-rendered dropdown problem.

Usage:
    python scraper/scrape_baseball.py
    python scraper/scrape_baseball.py --out docs/baseball_stats.json --year 2026
"""

import argparse
import io
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import pdfplumber
import requests

# ── CLI Args ──────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--out",  default="docs/baseball_stats.json")
parser.add_argument("--year", default="2026")
args = parser.parse_args()

YEAR     = args.year
OUT_PATH = Path(args.out)
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

PDF_URL = os.environ.get(
    "STATS_PDF_URL",
    f"https://s3.us-east-2.amazonaws.com/sidearm.nextgen.sites/"
    f"tommiesports.com/stats/baseball/{YEAR}/pdf/cume.pdf"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


# ── Helpers ───────────────────────────────────────────────────────────────────
def clean(val) -> str:
    return " ".join(str(val or "").split()).strip()


def build_by_number(rows: list[dict]) -> dict:
    return {r["number"]: r for r in rows if r.get("number")}


def normalise_row(row: dict, headers: list[str]) -> dict:
    """Normalise jersey number and player name keys."""
    number = (
        row.get("##") or row.get("#") or row.get("No")
        or row.get("No.") or row.get("NUM") or ""
    ).strip()
    player = (
        row.get("Player") or row.get("Name") or row.get("PLAYER") or ""
    ).strip()
    row["number"] = number
    row["player"] = player
    return row


def is_pitching_headers(headers: list[str]) -> bool:
    h = set(h.upper() for h in headers)
    return bool(h & {"ERA", "IP", "APP", "GS", "CG", "SHO", "SV"})


def is_hitting_headers(headers: list[str]) -> bool:
    h = set(h.upper() for h in headers)
    return bool(h & {"AVG", "AB", "OBP", "SLG", "OPS", "OB%", "SLG%"})


# ── PDF Parsing ───────────────────────────────────────────────────────────────
def parse_pdf(pdf_bytes: bytes) -> tuple[list[dict], list[dict]]:
    """
    Extract hitting and pitching tables from the cumulative stats PDF.
    pdfplumber extracts tables with bounding-box detection.
    """
    hitting_rows  = []
    pitching_rows = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        print(f"  📄  PDF has {len(pdf.pages)} page(s)")

        for page_num, page in enumerate(pdf.pages, 1):
            tables = page.extract_tables()
            print(f"  📃  Page {page_num}: found {len(tables)} table(s)")

            for table in tables:
                if not table or len(table) < 2:
                    continue

                # First non-empty row is the header
                headers = [clean(c) for c in table[0]]
                if not any(headers):
                    # Try second row as header
                    headers = [clean(c) for c in table[1]]
                    data_rows = table[2:]
                else:
                    data_rows = table[1:]

                print(f"    Headers: {headers[:8]}...")

                rows = []
                for raw_row in data_rows:
                    if not raw_row or all(c is None or clean(c) == "" for c in raw_row):
                        continue
                    cells = [clean(c) for c in raw_row]
                    # Pad to header length
                    while len(cells) < len(headers):
                        cells.append("")
                    row = dict(zip(headers, cells[:len(headers)]))
                    row = normalise_row(row, headers)
                    rows.append(row)

                if is_pitching_headers(headers):
                    print(f"    ✅  Classified as PITCHING ({len(rows)} rows)")
                    pitching_rows.extend(rows)
                elif is_hitting_headers(headers):
                    print(f"    ✅  Classified as HITTING ({len(rows)} rows)")
                    hitting_rows.extend(rows)
                else:
                    print(f"    ↩️  Skipped (unrecognised table type)")

    return hitting_rows, pitching_rows


# ── Main ──────────────────────────────────────────────────────────────────────
def scrape() -> dict:
    print(f"📥  Downloading PDF from:\n    {PDF_URL}")
    resp = requests.get(PDF_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    print(f"  ✅  Downloaded {len(resp.content):,} bytes")

    hitting_rows, pitching_rows = parse_pdf(resp.content)

    return {
        "generated_at":       datetime.now(timezone.utc).isoformat(),
        "status":             "ok",
        "season":             YEAR,
        "source_pdf":         PDF_URL,
        "hitting":            hitting_rows,
        "pitching":           pitching_rows,
        "hitting_by_number":  build_by_number(hitting_rows),
        "pitching_by_number": build_by_number(pitching_rows),
    }


def load_existing() -> dict:
    if OUT_PATH.exists():
        try:
            with open(OUT_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


if __name__ == "__main__":
    existing = load_existing()
    try:
        data = scrape()
        with open(OUT_PATH, "w") as f:
            json.dump(data, f, indent=2)
        print(f"\n✅  Saved → {OUT_PATH}")
        print(f"   Hitting rows:  {len(data['hitting'])}")
        print(f"   Pitching rows: {len(data['pitching'])}")
        if not data["pitching"]:
            print("   ⚠️  Pitching rows empty — check PDF structure above")
    except Exception:
        print("\n❌  Scrape failed — preserving last good data")
        traceback.print_exc()
        if existing:
            existing["status"] = "stale"
            existing["stale_since"] = datetime.now(timezone.utc).isoformat()
            with open(OUT_PATH, "w") as f:
                json.dump(existing, f, indent=2)
            print(f"   Stale data preserved at {OUT_PATH}")
        sys.exit(1)
