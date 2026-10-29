import json
import re
import sys
from bs4 import BeautifulSoup


def parse_devalue_payload(raw_text):
    """
    Parses a devalue/Nuxt serialized array payload into a resolved Python structure.
    In Nuxt/Sidearm data, root is [elements...], where integers act as pointers
    to other elements in the root array.
    """
    try:
        pool = json.loads(raw_text)
    except Exception as e:
        print(f"Error loading JSON data: {e}")
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
            # Recursively resolve referenced item
            target = pool[val]
            memo[val] = None  # placeholder against cycles
            resolved_target = resolve(target)
            memo[val] = resolved_target
            return resolved_target

        elif isinstance(val, list):
            return [resolve(x) for x in val]

        elif isinstance(val, dict):
            return {k: resolve(v) for k, v in val.items()}

        return val

    # Root data payload is usually at pool[0] or pool[1]
    return resolve(pool[0] if len(pool) > 0 else {})


def extract_player_stats(html_content):
    """Extracts Batting and Pitching stats and sorts players by jersey number."""
    soup = BeautifulSoup(html_content, "html.parser")

    # Locate the Nuxt data payload script
    payload_str = None
    scripts = soup.find_all("script")
    for script in scripts:
        text = script.string or ""
        if "__NUXT__" in text or "cumulativeStats" in text or "overallIndividualStats" in text:
            # Match JSON array pattern inside window.__NUXT__ or data definition
            match = re.search(r"(\[.*\])", text, re.DOTALL)
            if match:
                payload_str = match.group(1)
                break

    if not payload_str:
        print("Could not find embedded Nuxt payload.")
        return []

    data = parse_devalue_payload(payload_str)

    # Walk through the resolved data tree to find overallIndividualStats
    def find_key(obj, target_key):
        if isinstance(obj, dict):
            if target_key in obj:
                return obj[target_key]
            for v in obj.values():
                res = find_key(v, target_key)
                if res is not None:
                    return res
        elif isinstance(obj, list):
            for item in obj:
                res = find_key(item, target_key)
                if res is not None:
                    return res
        return None

    individual_stats = find_key(data, "overallIndividualStats")
    if not individual_stats or not isinstance(individual_stats, list):
        print("Could not locate player individual stats in payload.")
        return []

    players = []

    for entry in individual_stats:
        if not isinstance(entry, dict) or entry.get("isAFooterStat") == 1:
            continue

        raw_jersey = str(entry.get("playerUniform", "")).strip()
        player_name = str(entry.get("playerName", "")).strip()

        if not raw_jersey or not player_name or player_name == "-1":
            continue

        try:
            jersey_num = int(raw_jersey)
        except ValueError:
            jersey_num = 999  # Fallback for sorting non-numeric numbers

        # Clean string/numeric value helpers
        def clean_val(v, default="0"):
            if v is None or v == -1 or v == "-1":
                return default
            return str(v)

        # Batting stats
        batting_avg = clean_val(entry.get("battingAverage"), ".000")
        at_bats = clean_val(entry.get("atBats"), "0")
        runs = clean_val(entry.get("runs"), "0")
        hits = clean_val(entry.get("hits"), "0")
        doubles = clean_val(entry.get("doubles"), "0")
        triples = clean_val(entry.get("triples"), "0")
        home_runs = clean_val(entry.get("homeRuns"), "0")
        rbi = clean_val(entry.get("runsBattedIn"), "0")
        ops = clean_val(entry.get("ops"), ".000")
        slg = clean_val(entry.get("sluggingPercentage"), ".000")

        # Pitching stats
        era = clean_val(entry.get("earnedRunAverage"), "-")
        whip = clean_val(entry.get("whip"), "-")
        wins = clean_val(entry.get("wins"), "0")
        losses = clean_val(entry.get("losses"), "0")
        ip = clean_val(entry.get("inningsPitched"), "0.0")
        so = clean_val(entry.get("strikeoutsPitching", entry.get("strikeouts")), "0")

        wl = f"{wins}-{losses}"

        # Determine player roles
        has_batting = int(at_bats) > 0 or runs != "0" or hits != "0"
        has_pitching = era != "-" or ip != "0.0" or int(wins) > 0 or int(losses) > 0

        player_data = {
            "jersey_number": jersey_num,
            "player_name": player_name,
        }

        if has_batting:
            player_data["batting_stats"] = {
                "AVG": batting_avg,
                "R": runs,
                "H": hits,
                "2B": doubles,
                "3B": triples,
                "HR": home_runs,
                "RBI": rbi,
                "OPS": ops,
                "SLG%": slg
            }

        if has_pitching:
            player_data["pitching_stats"] = {
                "ERA": era,
                "WHIP": whip,
                "W-L": wl,
                "IP": ip,
                "SO": so
            }

        if has_batting or has_pitching:
            players.append(player_data)

    # Sort players by jersey number
    players.sort(key=lambda x: x["jersey_number"])
    return players


def main():
    input_file = sys.argv[1] if len(sys.argv) > 1 else "baseball_stats.html"
    output_file = sys.argv[2] if len(sys.argv) > 2 else "sorted_players_stats.json"

    print(f"Reading HTML from: {input_file}...")
    try:
        with open(input_file, "r", encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        print(f"File '{input_file}' not found.")
        return

    players = extract_player_stats(html)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(players, f, indent=2)

    print(f"Successfully scraped and exported {len(players)} players to '{output_file}' sorted by jersey number.")


if __name__ == "__main__":
    main()
