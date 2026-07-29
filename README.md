# Checkee Charts

Auto-updated US visa administrative processing dashboard, sourced from [checkee.info](https://www.checkee.info).

**Live dashboard → https://felixshen.github.io/checkee-charts**

![Dashboard Screenshot](screenshot.png?v=2)

## What it shows

### Visa group cards (top grid)

Six visa group bar charts plus summary cards.

| Card | Visas | Color |
|---|---|---|
| Business / Visitor | B1, B2 | Steel blue |
| Student | F1, F2 | Sage green |
| Work | H1, H4 | Coral red |
| Exchange Visitor | J1, J2 | Amber |
| Intracompany | L1, L2 | Teal |
| Extraordinary Ability | O1, O2 | Purple |

Each shows daily case counts (stacked bar) with a stats footer: total cases, median / min / max waiting days.

**Complete Date Distribution** — stacked bar (Clear / Reject) by complete date, with a dashed avg cases/day reference line.

**New vs Renewal** — total cases and average wait days by entry type (New / Renewal). Click to cross-filter.

**Consulate Distribution** — horizontal bar chart of the top 10 consulates by volume with median wait labels. Click a bar to cross-filter all charts and the table; click again to reset.

### Dynamic summary

The top summary line (total cases, clear %, median wait) recalculates dynamically when any filter is applied.

### 10-year trend charts (below grid)

**Monthly Cases (Trailing 10 Years)** — stacked % bar chart of Clear / Reject / Pending by month, overlaid with a total cases line. Includes a 🦠 COVID-19 marker.

**Avg Waiting Days (Trailing 10 Years)** — monthly average waiting days line, with shaded bands for each US presidential administration and a 🦠 COVID-19 marker.

### Records table

Sortable table of all raw records: Status, Check Date, Complete Date, Waiting Days, Visa Type, Entry, Consulate, Major, Details. Responds to all filter controls.

## How it works

1. `generate.py` scrapes the last 90 days from checkee.info and produces a self-contained `index.html`
2. `scrape_all.py` scrapes all months since Jan 2026 for a complete historical view
3. GitHub Actions runs daily at UTC 4:00 (noon CST), commits the updated HTML, and GitHub Pages serves it
4. Timestamps shown in CST (UTC+8)

### CI mode

In GitHub Actions (or any CI), the scripts use `undetected_chromedriver` in headless mode to bypass Cloudflare. Locally, they use a copied macOS Chrome profile with existing cookies.

```bash
# CI mode (auto-detected via CI=true env var, or pass --ci explicitly)
python generate.py --ci
python scrape_all.py --ci
```

## Run locally

Requires macOS with Google Chrome installed.

```bash
pip install -r requirements.txt
python generate.py          # last 90 days
python scrape_all.py        # all months since Jan 2026
open index.html
```
