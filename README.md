# ⚾ TAPbot — Summit League Stats Automation

**Built for St. Thomas Athletics · Powered by TAPbot-Xpression**

---

## What Is This?

TAPbot is an automated stats pipeline built for the St. Thomas Athletics department. Every morning — and whenever you need it — TAPbot goes out and grabs live baseball stats for the Tommies and their Summit League opponents, cleans them up, and makes them instantly available for broadcast graphics, scoreboard overlays, and Ross Xpression templates.

No more manually pulling stats from websites. No more copy-paste errors before a broadcast. TAPbot handles it automatically so your team can focus on the show.

---

## What Does It Actually Do?

Here's the simple version:

1. **Pulls live stats** from the official athletics websites for all 6 Summit League baseball programs
2. **Organizes the data** by player — batting stats, pitching stats, jersey number, position, and class year
3. **Saves everything** as clean, structured data files that Ross Xpression and other broadcast tools can read directly
4. **Updates automatically** every morning at 9:00 AM CT, or on-demand whenever you trigger it manually

---

## Which Schools Are Covered?

TAPbot tracks stats for all Summit League baseball programs:

| School | Team |
| :--- | :--- |
| 🟣 **St. Thomas** | Tommies |
| 🔵 South Dakota State | Jackrabbits |
| 🟢 North Dakota State | Bison |
| 🔵 Northern Colorado | Bears |
| 🟡 Oral Roberts | Golden Eagles |
| 🔴 Omaha | Mavericks |

---

## What Stats Are Tracked?

### ⚾ Batting
| Stat | What It Means |
| :--- | :--- |
| AVG | Batting Average |
| R | Runs Scored |
| H | Hits |
| 2B | Doubles |
| 3B | Triples |
| HR | Home Runs |
| RBI | Runs Batted In |
| OPS | On-base Plus Slugging |
| SLG% | Slugging Percentage |

### ⚡ Pitching
| Stat | What It Means |
| :--- | :--- |
| ERA | Earned Run Average |
| WHIP | Walks + Hits per Inning Pitched |
| W-L | Win-Loss Record |
| IP | Innings Pitched |
| SO | Strikeouts |

Each player entry also includes their **jersey number**, **position** (P, C, 1B, OF, etc.), and **class year** (Fr., So., Jr., Sr., Gr.) — exactly what you need for a broadcast lower-third or Xpression data feed.

---

## How Do I Use It for Broadcasts?

### For Ross Xpression Integration
The stats are stored as `.json` files in the `docs/` folder of this project. Your Xpression operator or graphics team can point their data feed directly at these files — either from the GitHub Pages URL or a local copy synced before the broadcast.

Each team has its own file:

```
docs/baseball_stats.json   ← St. Thomas (primary)
docs/stthomas.json         ← St. Thomas (alias)
docs/sdstate.json          ← South Dakota State
docs/ndsu.json             ← North Dakota State
docs/uncbears.json         ← Northern Colorado
docs/oru.json              ← Oral Roberts
docs/omaha.json            ← Omaha
docs/all_teams_stats.json  ← All 6 teams in one file
```

You can also look up any player by jersey number using the **live stats dashboard** hosted on GitHub Pages.

### For Broadcast Prep
Before a game, pull up the stats dashboard, type in a jersey number, and instantly see that player's full batting and pitching line — ranked against their teammates. Great for quickly building talking points or confirming stats before going on air.

---

## How Often Does It Update?

TAPbot runs on a schedule every day at **9:00 AM Central Time**. That means by the time your broadcast team is prepping for an afternoon or evening game, the stats are already current.

---
