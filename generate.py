#!/usr/bin/env python3
"""Scrapes checkee.info and generates index.html with daily visa case charts."""

import requests
from bs4 import BeautifulSoup
from collections import defaultdict
import json
import os
import re
import shutil
import statistics
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

CHROME_PROFILE_COPY = "/tmp/chrome-profile-copy"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Ch-Ua": '"Google Chrome";v="135", "Not-A.Brand";v="8", "Chromium";v="135"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
    "DNT": "1",
}

_session = None


def get_session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(HEADERS)
        # Warm up: visit homepage to establish cookies
        try:
            _session.get("https://www.checkee.info/", timeout=20)
            time.sleep(2)
        except Exception:
            pass
    return _session


def fetch_with_retry(url, retries=6, backoff=15):
    """GET with retries on 403/429/5xx using a persistent session for cookie handling."""
    session = get_session()
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=20)
            if r.status_code in (403, 429, 503) and attempt < retries - 1:
                print(f"  Got {r.status_code}, retrying in {backoff}s (attempt {attempt+1}/{retries})...")
                time.sleep(backoff)
                continue
            r.raise_for_status()
            return r
        except requests.exceptions.RequestException as e:
            if attempt < retries - 1:
                print(f"  Request error: {e}, retrying in {backoff}s...")
                time.sleep(backoff)
            else:
                raise


def parse_rows(soup):
    """Extract records from a BeautifulSoup page containing the checkee table."""
    records = []
    known_visas = {"B1", "B2", "F1", "F2", "H1", "H4", "J1", "J2", "L1", "L2", "O1"}
    known_statuses = {"Pending", "Clear", "Reject", "Approved", "Issued", "Refused"}
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) == 11:
            visa       = cells[2].get_text(strip=True)
            entry      = cells[3].get_text(strip=True)
            consulate  = cells[4].get_text(strip=True)
            major      = cells[5].get_text(strip=True)
            status     = cells[6].get_text(strip=True)
            check_date = cells[7].get_text(strip=True)
            date       = cells[8].get_text(strip=True)
            try:
                days = int(cells[9].get_text(strip=True))
            except ValueError:
                continue
            _dlink     = cells[10].find('a')
            details    = _dlink.get('title', '').strip() if _dlink else ''
            if re.match(r"^\d{4}-\d{2}-\d{2}$", date) and visa and 0 <= days < 2000:
                records.append({
                    "date": date,
                    "visa": visa,
                    "days": days,
                    "status": status,
                    "check_date": check_date,
                    "entry": entry,
                    "consulate": consulate,
                    "major": major,
                    "details": details,
                })
            continue

        texts = [c.get_text(" ", strip=True) for c in cells]
        if len(texts) < 8:
            continue

        visa_idx = next((i for i, t in enumerate(texts) if t in known_visas), None)
        status_idx = next((i for i, t in enumerate(texts) if t in known_statuses), None)
        date_items = [(i, t) for i, t in enumerate(texts) if re.match(r"^\d{4}-\d{2}-\d{2}$", t)]
        int_items = [(i, t) for i, t in enumerate(texts) if re.match(r"^\d+$", t)]
        if visa_idx is None or status_idx is None or len(date_items) < 2 or not int_items:
            continue

        days_idx, days_text = int_items[-1]
        try:
            days = int(days_text)
        except ValueError:
            continue
        if not (0 <= days < 2000):
            continue

        check_date = date_items[-2][1]
        date = date_items[-1][1]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            continue

        entry = texts[visa_idx + 1] if visa_idx + 1 < len(texts) else ""
        consulate = texts[visa_idx + 2] if visa_idx + 2 < len(texts) else ""
        major = texts[visa_idx + 3] if visa_idx + 3 < len(texts) else ""
        details_link = cells[-1].find("a") if cells else None
        details = details_link.get("title", "").strip() if details_link else ""
        records.append({
            "date": date,
            "visa": texts[visa_idx],
            "days": days,
            "status": texts[status_idx],
            "check_date": check_date,
            "entry": entry,
            "consulate": consulate,
            "major": major,
            "details": details,
        })
    return records


def describe_table_rows(soup, limit=8):
    """Return compact diagnostics for why parse_rows found no records."""
    rows = []
    counts = defaultdict(int)
    for row in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
        if cells:
            counts[len(cells)] += 1
            if len(rows) < limit:
                rows.append(cells[:12])
    return {
        "cell_counts": dict(sorted(counts.items())),
        "samples": rows,
    }


def save_debug_html(name, html):
    try:
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        path = log_dir / name
        path.write_text(html, encoding="utf-8")
        print(f"Saved debug page: {path}")
    except Exception as exc:
        print(f"WARNING: could not save debug page ({exc})")


def page_looks_blocked(html, title=""):
    """Detect common Cloudflare/security interstitial pages."""
    haystack = f"{title}\n{html[:200000]}".lower()
    markers = (
        "just a moment",
        "performing security verification",
        "cloudflare",
        "verify you are human",
        "verifying you are not a bot",
        "checking your browser",
    )
    return any(marker in haystack for marker in markers)


def wait_for_dispdate_selects(driver, by, timeout=90, interval=3):
    """Wait until the checkee filter form is available, allowing CF to finish first."""
    deadline = time.time() + timeout
    next_log = 0
    while True:
        selects = driver.find_elements(by.NAME, "dispdate")
        if len(selects) >= 2:
            return selects

        page_source = driver.page_source
        title = driver.title
        now = time.time()
        if now >= deadline:
            save_debug_html("last-selenium-before-submit.html", page_source)
            raise RuntimeError(f"Expected 2 dispdate selects, got {len(selects)}; title={title!r}")

        if now >= next_log:
            if page_looks_blocked(page_source, title):
                print(f"Waiting for security verification before submit... title={title!r}")
            else:
                print(f"Waiting for checkee filters before submit... title={title!r}")
            next_log = now + 12
        time.sleep(interval)


def wait_for_records_after_submit(driver, timeout=120, interval=3):
    """Wait until the submitted result page contains parseable table rows."""
    deadline = time.time() + timeout
    next_log = 0
    last_records = []
    last_soup = None
    last_source = ""

    while True:
        page_source = driver.page_source
        soup = BeautifulSoup(page_source, "html.parser")
        records = parse_rows(soup)
        if records:
            return records, page_source, soup

        last_records = records
        last_soup = soup
        last_source = page_source

        title = driver.title
        now = time.time()
        if now >= deadline:
            return last_records, last_source, last_soup

        if now >= next_log:
            if page_looks_blocked(page_source, title):
                print(f"Waiting for security verification after submit... title={title!r}")
            else:
                diag = describe_table_rows(soup)
                print(f"Waiting for result rows... title={title!r}; row cell counts={diag['cell_counts']}")
            next_log = now + 12
        time.sleep(interval)


def load_cached_records(html_path="index.html"):
    """Extract raw_records from the existing index.html DATA blob."""
    try:
        with open(html_path, encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return []
    m = re.search(r'const DATA\s*=\s*(\{.*?\});\s*\n', content, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    raw = data.get("raw_records", [])
    # raw_records format: [date, visa, days, status, check_date, consulate, entry, major, details]
    records = []
    for r in raw:
        if len(r) >= 9:
            records.append({
                "date": r[0], "visa": r[1], "days": r[2], "status": r[3],
                "check_date": r[4], "consulate": r[5], "entry": r[6],
                "major": r[7], "details": r[8],
            })
    return records


def build_chrome_options(force_refresh=False, ci=False):
    """Create Chrome options. In CI mode: headless, no profile. Local: copied macOS profile."""
    from selenium.webdriver.chrome.options import Options

    if ci:
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--disable-gpu")
        print("CI mode: headless Chrome, no profile")
        return opts

    # Local mode: copy Chrome profile for CF cookies
    src = os.path.expanduser("~/Library/Application Support/Google/Chrome/Default")
    dst = CHROME_PROFILE_COPY
    profile_age = float('inf')
    if os.path.exists(dst):
        profile_age = time.time() - os.path.getmtime(dst)

    if force_refresh or profile_age > 3600:
        if os.path.exists(dst):
            try:
                shutil.rmtree(dst)
            except Exception:
                pass
        if not os.path.exists(dst):
            shutil.copytree(src, dst,
                ignore=shutil.ignore_patterns(
                    'SingletonLock', 'SingletonCookie', 'SingletonSocket',
                    'GPUCache', 'ShaderCache', 'Code Cache', 'Cache',
                ))
        print(f"Chrome profile copied to {dst}")
    else:
        print(f"Using existing profile copy (age {profile_age:.0f}s)")

    opts = Options()
    opts.add_argument(f"--user-data-dir={dst}")
    opts.add_argument("--profile-directory=.")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    return opts


def _get_chrome_major_version():
    """Detect installed Chrome major version (Linux/macOS)."""
    import subprocess
    for cmd in ["google-chrome", "google-chrome-stable", "chromium-browser",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]:
        try:
            out = subprocess.check_output([cmd, "--version"], stderr=subprocess.DEVNULL).decode()
            m = re.search(r"(\d+)\.", out)
            if m:
                return int(m.group(1))
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return None


def _make_uc_driver(ci=False, force_refresh_profile=False):
    """Create a Chrome driver — undetected_chromedriver in CI, regular Selenium locally."""
    if ci:
        import undetected_chromedriver as uc
        opts = build_chrome_options(ci=True)
        ver = _get_chrome_major_version()
        print(f"Chrome major version: {ver}")
        kwargs = dict(options=opts, headless=True, use_subprocess=False)
        if ver:
            kwargs["version_main"] = ver
        return uc.Chrome(**kwargs)
    else:
        from selenium import webdriver
        return webdriver.Chrome(options=build_chrome_options(force_refresh=force_refresh_profile))


def scrape_with_selenium(force_refresh_profile=False, ci=False):
    """Submit the 90-day form on checkee.info and parse results.
    Local: non-headless Chrome with copied profile (CF cookies).
    CI: undetected_chromedriver in headless mode."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select as SeleniumSelect

    driver = _make_uc_driver(ci=ci, force_refresh_profile=force_refresh_profile)
    try:
        driver.get("https://www.checkee.info/main.php?sortby=clear_date")

        # Select the second dispdate dropdown (Last 7/30/90 Days)
        selects = wait_for_dispdate_selects(driver, By)
        sel90 = SeleniumSelect(selects[1])
        target_val = None
        for opt in sel90.options:
            if "90" in opt.text:
                target_val = opt.get_attribute("value")
                sel90.select_by_value(target_val)
                break
        if not target_val:
            raise RuntimeError("Could not find '90 Days' option in dropdown")
        print(f"Selected: Last 90 Days = {target_val}")

        # Submit the form containing the second select
        forms = driver.find_elements(By.XPATH, "//form[@action='./disppage.php']")
        if len(forms) >= 2:
            forms[1].submit()
        else:
            selects[1].find_element(By.XPATH, "./ancestor::form").submit()

        print(f"Landed on: {driver.current_url}")
        records, page_source, soup = wait_for_records_after_submit(driver)
        if not records:
            save_debug_html("last-selenium-page.html", page_source)
            print(f"Page title: {driver.title}")
            diag = describe_table_rows(soup)
            print(f"Row cell counts: {diag['cell_counts']}")
            for i, cells in enumerate(diag["samples"], 1):
                print(f"Row sample {i}: {cells}")
        return records
    finally:
        driver.quit()


def scrape_with_curl_cffi():
    """Fetch last 7 days from checkee.info using curl_cffi (TLS fingerprint impersonation).
    Works in CI where Cloudflare blocks regular requests and Selenium."""
    from curl_cffi import requests as cffi_requests
    session = cffi_requests.Session()
    session.get("https://www.checkee.info/", impersonate="chrome", timeout=20)
    time.sleep(2)
    r = session.get("https://www.checkee.info/main.php?sortby=clear_date",
                    impersonate="chrome", timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    records = parse_rows(soup)
    print(f"curl_cffi: {len(records)} records from 7-day view")
    return records


def _incremental_merge(fresh_records):
    """Merge fresh records with cached index.html data, deduplicate, and return."""
    cached_records = load_cached_records()
    print(f"Cached records from index.html: {len(cached_records)}")

    if not fresh_records:
        raise RuntimeError("No fresh rows; keeping existing index.html.")

    def rec_key(r):
        return (r["date"], r["visa"], r["days"], r["check_date"])

    seen = set()
    merged = []
    for r in fresh_records + cached_records:
        k = rec_key(r)
        if k not in seen:
            seen.add(k)
            merged.append(r)

    # Keep all records since 2026-01-01
    cutoff = "2026-01-01"
    merged = [r for r in merged if r["date"] >= cutoff]
    print(f"Merged records (since {cutoff}): {len(merged)}")

    if cached_records and len(merged) < len(cached_records) * 0.90:
        raise RuntimeError(
            f"Fallback would shrink from {len(cached_records)} to {len(merged)}; keeping existing index.html."
        )

    if not merged:
        raise RuntimeError("No records after merge.")
    return merged


def scrape(ci=False):
    """Fetch dataset from checkee.info.
    CI: curl_cffi (7-day) + incremental merge with cached data.
    Local: Selenium with Chrome profile, falls back to incremental on failure."""
    if ci:
        # CI mode: use curl_cffi (no Chrome/Selenium needed)
        for attempt in range(1, 4):
            try:
                records = scrape_with_curl_cffi()
                if records:
                    return _incremental_merge(records)
            except Exception as e:
                print(f"curl_cffi attempt {attempt} failed ({e})")
            if attempt < 3:
                time.sleep(10)
        raise RuntimeError("curl_cffi failed after 3 attempts")

    # Local mode: try Selenium first
    for attempt in range(1, 4):
        try:
            records = scrape_with_selenium(force_refresh_profile=attempt > 1, ci=False)
            print(f"Selenium scrape attempt {attempt}: {len(records)} records")
            if records:
                return records
            print("WARNING: Selenium returned 0 records")
        except Exception as e:
            print(f"WARNING: Selenium scrape attempt {attempt} failed ({e})")

        if attempt < 3:
            time.sleep(15)

    print("WARNING: Selenium failed after 3 attempts, falling back to incremental mode")

    # Fallback: scrape base page + merge with cached data
    base = fetch_with_retry("https://www.checkee.info/main.php?sortby=clear_date")
    base_soup = BeautifulSoup(base.text, "html.parser")
    fresh_records = parse_rows(base_soup)
    print(f"Fallback — fresh rows from base page: {len(fresh_records)}")

    return _incremental_merge(fresh_records)
    return merged


def monthly_dict_from_rows(rows):
    rows.sort(key=lambda x: x["month"])
    rows = rows[-120:]
    return {
        "months":   [r["month"]    for r in rows],
        "pending":  [r["pending"]  for r in rows],
        "clear":    [r["clear"]    for r in rows],
        "reject":   [r["reject"]   for r in rows],
        "total":    [r["total"]    for r in rows],
        "avg_wait": [r["avg_wait"] for r in rows],
    }


def parse_monthly_rows(soup):
    """Extract homepage monthly case rows from a BeautifulSoup page."""
    rows = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) >= 6:
            month = cells[1].get_text(strip=True)
            if not re.match(r"^\d{4}-\d{2}$", month):
                continue
            try:
                pending = int(cells[2].get_text(strip=True))
                clear   = int(cells[3].get_text(strip=True))
                reject  = int(cells[4].get_text(strip=True))
                total   = int(cells[5].get_text(strip=True))
                avg_wait_raw = cells[6].get_text(strip=True) if len(cells) > 6 else "-"
                avg_wait = float(avg_wait_raw) if avg_wait_raw not in ("-", "", "N/A") else None
            except ValueError:
                continue
            rows.append({"month": month, "pending": pending, "clear": clear, "reject": reject, "total": total, "avg_wait": avg_wait})
    return rows


def scrape_monthly_with_selenium(ci=False):
    """Scrape homepage monthly case table using Selenium."""
    driver = _make_uc_driver(ci=ci)
    try:
        driver.get("https://www.checkee.info/")
        for _ in range(3):
            time.sleep(5)
            soup = BeautifulSoup(driver.page_source, "html.parser")
            rows = parse_monthly_rows(soup)
            if rows:
                print(f"Selenium monthly rows: {len(rows)}")
                return monthly_dict_from_rows(rows)
        print(f"WARNING: Selenium monthly scrape returned 0 rows from {driver.current_url}")
    finally:
        driver.quit()
    return monthly_dict_from_rows([])


def scrape_monthly(ci=False):
    """Scrape homepage monthly case table; return trailing 120 months."""
    try:
        r = fetch_with_retry("https://www.checkee.info/")
        soup = BeautifulSoup(r.text, "html.parser")
        rows = parse_monthly_rows(soup)
        if rows:
            return monthly_dict_from_rows(rows)
        print("WARNING: monthly requests scrape returned 0 rows; trying Selenium")
    except Exception as e:
        print(f"WARNING: monthly requests scrape failed ({e}); trying Selenium")
    return scrape_monthly_with_selenium(ci=ci)


def build_data(records, monthly):
    if records:
        d0 = datetime.strptime(min(r["date"] for r in records), "%Y-%m-%d")
        d1 = datetime.strptime(max(r["date"] for r in records), "%Y-%m-%d")
        dates = [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((d1 - d0).days + 1)]
    else:
        dates = []

    counts = defaultdict(lambda: defaultdict(int))
    raw_days = defaultdict(list)
    complete_status_counts = defaultdict(lambda: defaultdict(int))  # complete_date -> status -> count
    entry_counts = defaultdict(int)
    entry_date_days = defaultdict(lambda: defaultdict(list))   # entry_type -> date -> [days]
    consulate_counts = defaultdict(int)
    consulate_days = defaultdict(list)

    for r in records:
        counts[r["visa"]][r["date"]] += 1
        raw_days[r["visa"]].append(r["days"])
        # Complete date distribution
        if re.match(r"^\d{4}-\d{2}-\d{2}$", r["date"]):
            complete_status_counts[r["date"]][r["status"]] += 1
        if r["entry"]:
            entry_counts[r["entry"]] += 1
            entry_date_days[r["entry"]][r["date"]].append(r["days"])
        if r["consulate"]:
            consulate_counts[r["consulate"]] += 1
            consulate_days[r["consulate"]].append(r["days"])

    groups_visas = [["B1", "B2"], ["F1", "F2"], ["H1", "H4"], ["J1", "J2"], ["L1", "L2"], ["O1", "O2"]]
    stats = {}
    for visas in groups_visas:
        all_days = [d for v in visas for d in raw_days[v]]
        key = ",".join(visas)
        stats[key] = {
            "total": len(all_days),
            "med": round(statistics.median(all_days)) if all_days else 0,
            "min": min(all_days) if all_days else 0,
            "max": max(all_days) if all_days else 0,
        }

    # New vs Renewal summary totals
    entry_types = sorted(entry_counts.keys())
    entry_summary = {}
    for et in entry_types:
        all_days = [d for dl in entry_date_days[et].values() for d in dl]
        entry_summary[et] = {
            "count": len(all_days),
            "avg_days": round(sum(all_days) / len(all_days), 1) if all_days else 0,
        }

    # Complete date distribution: sorted statuses for consistent coloring
    all_statuses = sorted(set(
        s for day_s in complete_status_counts.values() for s in day_s
    ))
    complete_dist = {
        "dates": dates,
        "statuses": all_statuses,
        "counts": {
            s: {d: complete_status_counts[d].get(s, 0) for d in dates}
            for s in all_statuses
        }
    }

    # Compact raw records: [date, visa, days, status, check_date, consulate, entry, major, details]
    raw_records = [
        [r["date"], r["visa"], r["days"], r["status"], r["check_date"],
         r["consulate"], r["entry"], r["major"], r["details"]]
        for r in records
    ]

    clear_days_all = [r["days"] for r in records if r["status"] == "Clear"]
    total_count = len(records)
    clear_count = sum(1 for r in records if r["status"] == "Clear")

    return {
        "dates": dates,
        "counts": {v: dict(d) for v, d in counts.items()},
        "stats": stats,
        "entry_summary": entry_summary,
        "complete_dist": complete_dist,
        "entry_dist": dict(entry_counts),
        "consulate_dist": dict(consulate_counts),
        "consulate_median": {k: round(statistics.median(v)) for k, v in consulate_days.items() if v},
        "raw_records": raw_records,
        "monthly": monthly,
        "summary": {
            "total": total_count,
            "clear_pct": round(100 * clear_count / total_count, 1) if total_count else 0,
            "med_wait": round(statistics.median(clear_days_all)) if clear_days_all else 0,
        },
    }


def generate_html(data, updated, date_label="Last 90 Days"):
    s = data.get("summary", {})
    summary_html = (
        f'{s.get("total", 0):,} cases ({date_label})'
        f' &nbsp;·&nbsp; {s.get("clear_pct", 0)}% eventually cleared'
        f' &nbsp;·&nbsp; median wait {s.get("med_wait", 0)}d'
    )
    data_json = json.dumps(data)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Checkee.info — Daily Visa Case Charts</title>
<link rel="icon" href="https://www.checkee.info/favicon.ico">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3/dist/chartjs-plugin-annotation.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #f8f7f4; font-family: 'Inter', Arial, sans-serif; color: #1e293b; }}
  h1 {{ text-align: center; font-size: 24px; font-weight: 700; padding: 24px 0 6px; letter-spacing: -0.3px; color: #1e293b; }}
  .updated {{ text-align: center; font-size: 11px; color: #94a3b8; margin-bottom: 4px; }}
  .updated a {{ color: #94a3b8; }}
  .summary-stats {{ text-align: center; font-size: 12px; color: #64748b; margin-bottom: 14px; }}
  .filter-bar {{
    display: flex; justify-content: center; align-items: end; gap: 10px;
    flex-wrap: wrap; max-width: 1100px; margin: 0 auto 14px; padding: 0 16px;
  }}
  .filter-control {{ display: flex; min-width: 150px; }}
  .filter-select, .filter-summary {{
    min-height: 32px; border: 1px solid #d8d6cf; background: #fff; color: #1e293b;
    border-radius: 6px; padding: 5px 9px; font-size: 12px; font-weight: 500;
  }}
  .filter-select {{ cursor: pointer; }}
  .filter-dropdown {{ position: relative; }}
  .filter-dropdown summary {{ list-style: none; cursor: pointer; user-select: none; min-width: 220px; }}
  .filter-dropdown summary::-webkit-details-marker {{ display: none; }}
  .filter-summary::after {{ content: '▾'; float: right; color: #94a3b8; margin-left: 12px; }}
  .filter-menu {{
    position: absolute; z-index: 20; top: calc(100% + 4px); left: 0; min-width: 220px;
    max-height: 260px; overflow-y: auto; background: #fff; border: 1px solid #d8d6cf;
    border-radius: 8px; box-shadow: 0 10px 24px rgba(15,23,42,0.14); padding: 6px;
  }}
  .filter-option {{ display: flex; align-items: center; gap: 7px; padding: 6px 7px; border-radius: 5px; font-size: 12px; cursor: pointer; }}
  .filter-option:hover {{ background: #f1f5f9; }}
  .filter-option input {{ margin: 0; }}
  .filter-option.all {{ font-weight: 700; border-bottom: 1px solid #e2e8f0; margin-bottom: 4px; padding-bottom: 8px; }}
  .grid  {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; padding: 0 20px 24px; max-width: 1500px; margin: 0 auto; align-items: start; }}
  .monthly-wrap {{ padding: 0 20px 24px; max-width: 1500px; margin: 0 auto; }}
  .card {{ background: #fff; border-radius: 8px; padding: 14px; box-shadow: 0 1px 6px rgba(0,0,0,0.08); }}
  .card h3 {{ text-align: center; font-size: 13px; font-weight: 600; margin-bottom: 8px; color: #1e293b; }}
  .stats {{ display: flex; justify-content: center; gap: 14px; flex-wrap: wrap; margin-top: 8px; font-size: 11px; color: #888; border-top: 1px solid #f0f0f0; padding-top: 7px; }}
  @media (max-width: 900px)  {{ .grid  {{ grid-template-columns: repeat(2, 1fr); }} }}
  @media (max-width: 600px)  {{ .grid {{ grid-template-columns: 1fr; padding: 0 12px 16px; gap: 12px; }} .monthly-wrap {{ padding: 0 12px 16px; }} h1 {{ font-size: 18px; padding: 16px 12px 4px; }} .outer-wrap {{ padding: 0 12px; }} }}
  .table-scroll {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
/* ── Records table ─────────────────────────────────────────────── */
#recordsTable {{ width:100%; border-collapse:collapse; font-size:12px; table-layout:fixed; }}
#recordsTable thead th {{
  position: sticky; top: 0;
  background: #f0f0f0; border-bottom: 2px solid #ddd;
  padding: 6px 8px; text-align: left; white-space: normal;
  cursor: pointer; user-select: none;
}}
#recordsTable thead th:hover {{ background: #e4e4e4; }}
#recordsTable tbody tr:nth-child(even) {{ background: #f9f9f9; }}
#recordsTable tbody tr:hover {{ background: #eef4ff; }}
#recordsTable tbody td {{
  padding: 5px 8px; border-bottom: 1px solid #eee;
  vertical-align: top; overflow: hidden;
}}
#recordsTable td.col-details {{ white-space: pre-wrap; word-break: break-word; font-size:11px; }}
#recordsTable td.col-major {{ white-space: normal; word-break: break-word; }}
#recordsTable td:not(.col-details):not(.col-major) {{ white-space: nowrap; text-overflow: ellipsis; }}
#recordsTable td.col-days, #recordsTable th.col-days,
#recordsTable td.col-center, #recordsTable th.col-center {{ text-align: center !important; }}
.table-sort-asc::after  {{ content: ' ▲'; font-size:10px; }}
.table-sort-desc::after {{ content: ' ▼'; font-size:10px; }}
#tableCount {{ font-size:12px; color:#888; margin-bottom:6px; }}
</style>
</head>
<body>
<h1>Daily Completed Cases by Visa Category ({date_label})</h1>
<p class="updated">Last updated: {updated} &nbsp;·&nbsp; Source: <a href="https://www.checkee.info" target="_blank">checkee.info</a></p>
<p class="summary-stats" id="summaryStats">{summary_html}</p>
<div class="filter-bar" id="filterBar">
  <div class="filter-control">
    <details class="filter-dropdown" id="visaDropdown">
      <summary class="filter-summary" id="visaSummary">All visa types</summary>
      <div class="filter-menu" id="visaMenu"></div>
    </details>
  </div>
  <label class="filter-control">
    <select class="filter-select" id="entrySelect"></select>
  </label>
  <label class="filter-control">
    <select class="filter-select" id="consulateSelect"></select>
  </label>
</div>
<div class="grid" id="grid"></div>
<div class="monthly-wrap" id="monthlyWrap"></div>
<div class="monthly-wrap" id="waitWrap"></div>
<div class="outer-wrap" style="max-width:1500px;margin:0 auto 24px;padding:0 20px">
  <div class="card">
    <h3>All Records ({date_label})</h3>
    <div id="tableCount"></div>
    <div class="table-scroll"><table id="recordsTable"><thead></thead><tbody></tbody></table></div>
  </div>
</div>
<script>
const DATA = {data_json};
const DATE_LABEL = '{date_label}';
const groups = [
  {{ label: 'Business / Visitor', visas: ['B1','B2'], colors: ['#4E79A7','#9CBDDB'] }},
  {{ label: 'Student',            visas: ['F1','F2'], colors: ['#59A14F','#97CB8F'] }},
  {{ label: 'Work',               visas: ['H1','H4'], colors: ['#E15759','#F0AAAB'] }},
  {{ label: 'Exchange Visitor',   visas: ['J1','J2'], colors: ['#F28E2B','#F8BF80'] }},
  {{ label: 'Intracompany',       visas: ['L1','L2'], colors: ['#76B7B2','#AADBD7'] }},
  {{ label: 'Extraordinary Ability', visas: ['O1', 'O2'], colors: ['#B07AA1', '#D4B5CC'] }},
];

// Status colors (defined early so updateAllCharts can reference them)
const statusColors = {{}};
const palette = ['#54A06B','#D4635A','#9DB0C8','#A07840','#A86878','#4E6A7A','#7A9AAA','#A09030'];
(DATA.complete_dist.statuses || []).forEach((s, i) => {{
  statusColors[s] = palette[i % palette.length];
}});

const grid = document.getElementById('grid');
const visaDropdown = document.getElementById('visaDropdown');
const visaMenu = document.getElementById('visaMenu');
const visaSummary = document.getElementById('visaSummary');
const entrySelect = document.getElementById('entrySelect');
const consulateSelect = document.getElementById('consulateSelect');
const chartInstances = {{}};
let activeConsulate = null;
let activeEntryType = null;
let activeVisas = null;

const visaOrder = groups.flatMap(g => g.visas);
const allVisaTypes = visaOrder.filter(v => DATA.raw_records.some(r => r[1] === v));
const allEntryTypes = [...new Set(DATA.raw_records.map(r => r[6]).filter(Boolean))].sort();
const allConsulates = [...new Set(DATA.raw_records.map(r => r[5]).filter(Boolean))].sort();

function fillSelect(select, allLabel, values) {{
  select.innerHTML = '';
  const allOpt = document.createElement('option');
  allOpt.value = '';
  allOpt.textContent = allLabel;
  select.appendChild(allOpt);
  values.forEach(v => {{
    const opt = document.createElement('option');
    opt.value = v;
    opt.textContent = v;
    select.appendChild(opt);
  }});
}}

function syncFilterControls() {{
  entrySelect.value = activeEntryType || '';
  consulateSelect.value = activeConsulate || '';
  const selected = activeVisas === null ? [] : [...activeVisas];
  if (activeVisas === null) visaSummary.textContent = 'All visa types';
  else if (!selected.length) visaSummary.textContent = 'None selected';
  else if (selected.length === 1) visaSummary.textContent = selected[0];
  else visaSummary.textContent = selected.join(', ');
  visaMenu.querySelectorAll('input[data-visa]').forEach(input => {{
    input.checked = !activeVisas || activeVisas.has(input.dataset.visa);
  }});
  const allInput = visaMenu.querySelector('input[data-all]');
  if (allInput) allInput.checked = activeVisas === null;
}}

function applyFilters() {{
  syncFilterControls();
  updateAllCharts(getFilteredRecords());
}}

function setVisaFilter(visas) {{
  activeVisas = visas === null ? null : new Set(visas || []);
  applyFilters();
}}

function setEntryFilter(entryType) {{
  activeEntryType = entryType || null;
  applyFilters();
}}

function setConsulateFilter(consulate) {{
  activeConsulate = consulate || null;
  applyFilters();
}}

function renderFilters() {{
  fillSelect(entrySelect, 'All entry types', allEntryTypes);
  fillSelect(consulateSelect, 'All consulates', allConsulates);

  visaMenu.innerHTML = '';
  const allLabel = document.createElement('label');
  allLabel.className = 'filter-option all';
  allLabel.innerHTML = '<input type="checkbox" data-all> <span>All visa types</span>';
  allLabel.querySelector('input').addEventListener('change', (evt) => setVisaFilter(evt.target.checked ? null : []));
  visaMenu.appendChild(allLabel);

  allVisaTypes.forEach(v => {{
    const label = document.createElement('label');
    label.className = 'filter-option';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.dataset.visa = v;
    input.addEventListener('change', () => {{
      const checked = [...visaMenu.querySelectorAll('input[data-visa]:checked')].map(el => el.dataset.visa);
      setVisaFilter(checked.length === allVisaTypes.length ? null : checked);
    }});
    const text = document.createElement('span');
    text.textContent = v;
    label.appendChild(input);
    label.appendChild(text);
    visaMenu.appendChild(label);
  }});

  entrySelect.addEventListener('change', () => setEntryFilter(entrySelect.value));
  consulateSelect.addEventListener('change', () => setConsulateFilter(consulateSelect.value));
  syncFilterControls();
}}

function getFilteredRecords() {{
  return DATA.raw_records.filter(r =>
    (!activeVisas || activeVisas.has(r[1])) &&
    (!activeConsulate || r[5] === activeConsulate) &&
    (!activeEntryType  || r[6] === activeEntryType)
  );
}}

// ── Median helper ─────────────────────────────────────────────────────────────
function jsMedian(arr) {{
  if (!arr.length) return 0;
  const s = [...arr].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : Math.round((s[m - 1] + s[m]) / 2);
}}

// ── Client-side aggregation ───────────────────────────────────────────────────
function buildAgg(records) {{
  const dateSets = new Set();
  const countsMap = {{}};
  const rawDays = {{}};
  const compMap = {{}};
  const entryDays = {{}};  // entry_type -> [days]
  const consulateCounts = {{}};
  const consulateDays = {{}};

  for (const [date, visa, days, status, , consulate, entry] of records) {{
    dateSets.add(date);
    countsMap[visa] = countsMap[visa] || {{}};
    countsMap[visa][date] = (countsMap[visa][date] || 0) + 1;
    rawDays[visa] = rawDays[visa] || [];
    rawDays[visa].push(days);
    if (/^\\d{{4}}-\\d{{2}}-\\d{{2}}$/.test(date)) {{
      compMap[date] = compMap[date] || {{}};
      compMap[date][status] = (compMap[date][status] || 0) + 1;
    }}
    if (entry) {{
      entryDays[entry] = entryDays[entry] || [];
      entryDays[entry].push(days);
    }}
    if (consulate) {{
      consulateCounts[consulate] = (consulateCounts[consulate] || 0) + 1;
      consulateDays[consulate] = consulateDays[consulate] || [];
      consulateDays[consulate].push(days);
    }}
  }}

  const _sortedDates = [...dateSets].sort();
  const dates = [];
  if (_sortedDates.length >= 2) {{
    const _dStart = new Date(_sortedDates[0] + 'T12:00:00Z');
    const _dEnd   = new Date(_sortedDates[_sortedDates.length - 1] + 'T12:00:00Z');
    for (let _d = new Date(_dStart); _d <= _dEnd; _d.setUTCDate(_d.getUTCDate() + 1))
      dates.push(_d.toISOString().slice(0, 10));
  }} else {{
    dates.push(..._sortedDates);
  }}

  const stats = {{}};
  groups.forEach(g => {{
    const allDays = g.visas.flatMap(v => rawDays[v] || []);
    const tot = allDays.length;
    stats[g.visas.join(',')] = {{
      total: tot,
      med:   jsMedian(allDays),
      min:   tot ? Math.min(...allDays) : 0,
      max:   tot ? Math.max(...allDays) : 0,
    }};
  }});

  // New vs Renewal summary totals
  const _entryTypes = Object.keys(entryDays).sort();
  const entry_summary = Object.fromEntries(_entryTypes.map(et => {{
    const dl = entryDays[et] || [];
    return [et, {{
      count: dl.length,
      avg_days: dl.length ? +(dl.reduce((a, b) => a + b, 0) / dl.length).toFixed(1) : 0,
    }}];
  }}));

  // Use the global status list so colors stay consistent even if a consulate
  // has zero records for some statuses.
  const allStatuses = DATA.complete_dist.statuses || [];
  const complete_dist = {{
    dates:    dates,
    statuses: allStatuses,
    counts:   Object.fromEntries(allStatuses.map(s => [
      s, Object.fromEntries(dates.map(d => [d, (compMap[d] || {{}})[s] || 0]))
    ])),
  }};

  const consulate_median = Object.fromEntries(Object.entries(consulateDays).map(([k, dl]) => [k, jsMedian(dl)]));

  return {{
    dates, counts: countsMap, stats, entry_summary, complete_dist,
    consulate_dist: consulateCounts,
    consulate_median: consulate_median,
  }};
}}

// ── Refresh all charts from a (possibly filtered) record list ─────────────────
function updateAllCharts(records) {{
  const agg = buildAgg(records);

  // Update top summary stats based on filtered records
  const total = records.length;
  const clearCount = records.filter(r => r[3] === 'Clear').length;
  const clearPct = total ? (100 * clearCount / total).toFixed(1) : 0;
  const clearDays = records.filter(r => r[3] === 'Clear').map(r => r[2]);
  const medWait = jsMedian(clearDays);
  const summaryEl = document.getElementById('summaryStats');
  if (summaryEl) {{
    summaryEl.innerHTML =
      total.toLocaleString() + ' cases (' + DATE_LABEL + ')' +
      ' &nbsp;&middot;&nbsp; ' + clearPct + '% eventually cleared' +
      ' &nbsp;&middot;&nbsp; median wait ' + medWait + 'd';
  }}

  // Visa group charts (cards 0-5)
  groups.forEach((g, i) => {{
    const chart = chartInstances['c' + i];
    if (!chart) return;
    const s = agg.stats[g.visas.join(',')] || {{}};
    chart.data.labels = agg.dates;
    chart.data.datasets.forEach((ds, vi) => {{
      const v = g.visas[vi];
      ds.data = agg.dates.map(d => (agg.counts[v] || {{}})[d] || 0);
    }});
    chart.update();
    // Refresh stats footer
    const statsEl = chart.canvas.closest('.card').querySelector('.stats');
    if (statsEl) statsEl.innerHTML =
      '<span>n=<b style="color:#555">' + (s.total || 0) + '</b></span>' +
      '<span>med <b style="color:#888">' + (s.med || 0) + 'd</b></span>' +
      '<span>min <b style="color:#888">' + (s.min || 0) + 'd</b></span>' +
      '<span>max <b style="color:#888">' + (s.max || 0) + 'd</b></span>';
  }});

  // New vs Renewal summary chart
  const wc = chartInstances['cWait'];
  if (wc) {{
    const es = agg.entry_summary;
    const types = Object.keys(es).sort();
    wc.data.labels = types;
    wc.data.datasets[0].data = types.map(et => es[et].count);
    wc.data.datasets[0].backgroundColor = types.map((et, i) =>
      activeEntryType && et !== activeEntryType ? (['#4E79A733','#F28E2B33'])[i % 2] : (['#4E79A7CC','#F28E2BCC'])[i % 2]
    );
    wc.data.datasets[1].data = types.map(et => es[et].avg_days);
    wc.update();
  }}

  // Issue date distribution chart
  const cdc = chartInstances['cCD'];
  if (cdc) {{
    const cd = agg.complete_dist;
    cdc.data.labels = cd.dates;
    cdc.data.datasets = (cd.statuses || []).map(s => ({{
      label: s,
      data:  cd.dates.map(d => (cd.counts[s] || {{}})[d] || 0),
      backgroundColor: statusColors[s] || '#999',
      stack: 'stack',
      order: 1,
      pointStyle: 'rect',
    }}));
    // Recalculate avg and update annotation + footer
    const dynTotals = cd.dates.map(d => (cd.statuses || []).reduce((s, st) => s + ((cd.counts[st] || {{}})[d] || 0), 0));
    const dynAvg = dynTotals.length ? +(dynTotals.reduce((a, b) => a + b, 0) / dynTotals.length).toFixed(1) : 0;
    if (cdc.options.plugins.annotation && cdc.options.plugins.annotation.annotations.avgLine) {{
      cdc.options.plugins.annotation.annotations.avgLine.yMin = dynAvg;
      cdc.options.plugins.annotation.annotations.avgLine.yMax = dynAvg;
    }}
    const cdStats = document.getElementById('cdStats');
    if (cdStats) cdStats.innerHTML =
      '<span style="color:#aaa;font-size:10px">stacked bars = status by complete date &nbsp;·&nbsp; <b style="color:#1e293b">- - -</b> avg ' + dynAvg + ' cases/day</span>';
    cdc.update();
  }}
  updateConsulateChart(agg.consulate_dist, agg.consulate_median);
  _currentTableRecords = records;
  renderTable(records);
}}

// ── Cards 0-5: visa group bar charts ─────────────────────────────────────────
groups.forEach((g, i) => {{
  const s = DATA.stats[g.visas.join(',')] || {{}};
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML =
    '<h3>' + g.label + ' (' + g.visas.join(', ') + ')</h3>' +
    '<div style="position:relative;height:200px"><canvas id="c' + i + '"></canvas></div>' +
    '<div class="stats">' +
      '<span>n=<b style="color:#555">' + s.total + '</b></span>' +
      '<span>med <b style="color:#888">' + s.med + 'd</b></span>' +
      '<span>min <b style="color:#888">' + s.min + 'd</b></span>' +
      '<span>max <b style="color:#888">' + s.max + 'd</b></span>' +
    '</div>';
  grid.appendChild(card);

  chartInstances['c' + i] = new Chart(document.getElementById('c' + i), {{
    type: 'bar',
    data: {{
      labels: DATA.dates,
      datasets: g.visas.map((v, vi) => ({{
        label: v,
        data: DATA.dates.map(d => (DATA.counts[v] || {{}})[d] || 0),
        backgroundColor: g.colors[vi],
        stack: 'stack'
      }}))
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{
        legend: {{ position: 'top', labels: {{ font: {{ size: 11 }}, padding: 6 }} }},
        tooltip: {{ mode: 'index', intersect: false }}
      }},
      scales: {{
        x: {{ stacked: true, ticks: {{ maxRotation: 60, font: {{ size: 8 }} }} }},
        y: {{ stacked: true, beginAtZero: true, title: {{ display: true, text: '# Cases', font: {{ size: 10 }} }} }}
      }}
    }}
  }});
}});

// ── Card 6: New vs Renewal summary — total cases + avg wait ──────────────────
const waitCard = document.createElement('div');
waitCard.className = 'card';
waitCard.innerHTML =
  '<h3>New vs Renewal — Total Cases &amp; Avg Wait</h3>' +
  '<div style="position:relative;height:200px"><canvas id="cWait"></canvas></div>' +
  '<div class="stats"><span style="color:#aaa;font-size:10px">bars = total cases (left) &nbsp;·&nbsp; line = avg wait days (right)</span></div>';
grid.appendChild(waitCard);

(function() {{
  const es = DATA.entry_summary || {{}};
  const types = Object.keys(es).sort();
  chartInstances['cWait'] = new Chart(document.getElementById('cWait'), {{
    data: {{
      labels: types,
      datasets: [
        {{
          type: 'bar',
          label: '# Cases',
          data: types.map(et => es[et].count),
          backgroundColor: ['#4E79A7CC', '#F28E2BCC'],
          yAxisID: 'yCases',
          order: 2,
          borderRadius: 4,
        }},
        {{
          type: 'line',
          label: 'Avg Wait Days',
          data: types.map(et => es[et].avg_days),
          borderColor: '#1e293b',
          backgroundColor: 'transparent',
          borderWidth: 2,
          pointRadius: 6,
          pointHoverRadius: 8,
          pointBackgroundColor: ['#2c5f8a', '#b45309'],
          tension: 0,
          yAxisID: 'yDays',
          order: 1,
        }},
      ],
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      onClick: (evt, elements) => {{
        if (!elements.length) return;
        const et = types[elements[0].index];
        setEntryFilter(activeEntryType === et ? null : et);
      }},
      plugins: {{
        legend: {{ position: 'top', labels: {{ font: {{ size: 11 }}, padding: 6 }} }},
        tooltip: {{
          callbacks: {{
            label: (ctx) => {{
              const v = ctx.parsed.y;
              return ctx.dataset.type === 'line' ? 'Avg wait: ' + v + 'd' : 'Cases: ' + v;
            }}
          }}
        }}
      }},
      scales: {{
        x: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 13, weight: '600' }} }} }},
        yCases: {{ type: 'linear', position: 'left', beginAtZero: true,
          title: {{ display: true, text: '# Cases', font: {{ size: 10 }} }},
          grid: {{ color: 'rgba(0,0,0,0.05)' }},
          afterFit: ax => {{ ax.width = 52; }} }},
        yDays:  {{ type: 'linear', position: 'right', beginAtZero: true,
          title: {{ display: true, text: 'Avg Wait Days', font: {{ size: 10 }} }},
          grid: {{ drawOnChartArea: false }},
          afterFit: ax => {{ ax.width = 58; }} }},
      }},
    }},
  }});
}})();

// ── Card 7: complete date distribution (appended before waitCard) ─────────────
const cdCard = document.createElement('div');
cdCard.className = 'card';
cdCard.innerHTML = '<h3>Complete Date Distribution</h3><div style="position:relative;height:200px"><canvas id="cCD"></canvas></div>' +
  '<div class="stats" id="cdStats"></div>';
grid.insertBefore(cdCard, waitCard);

const cd = DATA.complete_dist;
const cdTotals = cd.dates.map(d => (cd.statuses || []).reduce((s, st) => s + ((cd.counts[st] || {{}})[d] || 0), 0));
const cdAvg = cdTotals.length ? +(cdTotals.reduce((a, b) => a + b, 0) / cdTotals.length).toFixed(1) : 0;
document.getElementById('cdStats').innerHTML =
  '<span style="color:#aaa;font-size:10px">stacked bars = status by complete date &nbsp;·&nbsp; <b style="color:#1e293b">- - -</b> avg ' + cdAvg + ' cases/day</span>';
chartInstances['cCD'] = new Chart(document.getElementById('cCD'), {{
  type: 'bar',
  data: {{
    labels: cd.dates,
    datasets: (cd.statuses || []).map(s => ({{
      label: s,
      data: cd.dates.map(d => (cd.counts[s] || {{}})[d] || 0),
      backgroundColor: statusColors[s],
      stack: 'stack',
      order: 1,
      pointStyle: 'rect',
    }})),
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{
      legend: {{ position: 'top', labels: {{ font: {{ size: 11 }}, padding: 6, usePointStyle: true }} }},
      tooltip: {{ mode: 'index', intersect: false }},
      annotation: {{ annotations: {{
        avgLine: {{
          type: 'line',
          yMin: cdAvg, yMax: cdAvg,
          borderColor: '#1e293b',
          borderWidth: 1.5,
          borderDash: [6, 3],
          label: {{ display: false }},
        }},
      }} }},
    }},
    scales: {{
      x: {{ stacked: true, ticks: {{ maxRotation: 60, font: {{ size: 8 }} }} }},
      y: {{ stacked: true, beginAtZero: true, title: {{ display: true, text: '# Cases', font: {{ size: 10 }} }} }}
    }}
  }}
}});

// ── Card 8: consulate pie — click to cross-filter ─────────────────────────────
const entryCard = document.createElement('div');
entryCard.className = 'card';
entryCard.innerHTML =
  '<h3>Consulate Distribution</h3>' +
  '<div style="position:relative;height:200px"><canvas id="cEntry"></canvas></div>' +
  '<div class="stats"><span style="color:#aaa;font-size:10px">click a bar · click again to reset</span></div>';
grid.appendChild(entryCard);

const TOP_N = 10;
let consLabels = [];
let consValues = [];
let consTotal = 0;
let consColors = [];
let currentConsMedians = {{}};

function setConsulateSeries(consDist, medians) {{
  const dist = consDist || {{}};
  currentConsMedians = medians || {{}};
  const labels = Object.keys(dist).sort((a, b) => dist[b] - dist[a]).slice(0, TOP_N);
  consLabels = labels;
  consValues = labels.map(k => dist[k]);
  consTotal = Object.values(dist).reduce((a, b) => a + b, 0);
  consColors = labels.map(() => '#4E79A7');
}}

function updateConsulateChart(consDist, medians) {{
  if (!chartInstances['cEntry']) return;
  setConsulateSeries(consDist, medians);
  chartInstances['cEntry'].data.labels = consLabels;
  chartInstances['cEntry'].data.datasets[0].data = consValues;
  chartInstances['cEntry'].data.datasets[0].backgroundColor = consLabels.map((l, i) =>
    activeConsulate && l !== activeConsulate ? consColors[i] + '33' : consColors[i] + 'CC'
  );
  chartInstances['cEntry'].data.datasets[0].hoverBackgroundColor = consColors;
  chartInstances['cEntry'].update();
}}

setConsulateSeries(DATA.consulate_dist, DATA.consulate_median);

chartInstances['cEntry'] = new Chart(document.getElementById('cEntry'), {{
  type: 'bar',
  plugins: [{{
    id: 'medianLabels',
    afterDatasetDraw(chart) {{
      const {{ctx, data}} = chart;
      const medians = currentConsMedians || {{}};
      ctx.save();
      ctx.font = '9px Inter, Arial, sans-serif';
      ctx.fillStyle = '#94a3b8';
      ctx.textAlign = 'left';
      data.labels.forEach((label, i) => {{
        const meta = chart.getDatasetMeta(0);
        const bar = meta.data[i];
        const med = medians[label];
        if (med !== undefined) ctx.fillText(med + 'd', bar.x + 5, bar.y + 4);
      }});
      ctx.restore();
    }}
  }}],
  data: {{
    labels: consLabels,
    datasets: [{{
      data: consValues,
      backgroundColor: consColors.map(c => c + 'CC'),
      hoverBackgroundColor: consColors,
      borderWidth: 0,
      borderRadius: 3,
    }}]
  }},
  options: {{
    indexAxis: 'y',
    responsive: true,
    maintainAspectRatio: false,
    onClick: (evt, elements) => {{
      if (!elements.length) return;
      const consulate = consLabels[elements[0].index];
      setConsulateFilter(activeConsulate === consulate ? null : consulate);
    }},
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{
        callbacks: {{
          label: (ctx) => {{
            const pct = ((ctx.parsed.x / consTotal) * 100).toFixed(1);
            const med = (currentConsMedians || {{}})[consLabels[ctx.dataIndex]];
            return ctx.parsed.x + ' cases (' + pct + '%)' + (med !== undefined ? ' · median ' + med + 'd wait' : '');
          }}
        }}
      }}
    }},
    scales: {{
      x: {{
        beginAtZero: true,
        grid: {{ color: 'rgba(0,0,0,0.05)' }},
        ticks: {{ font: {{ size: 9 }}, color: '#aaa' }},
      }},
      y: {{
        grid: {{ display: false }},
        ticks: {{ font: {{ size: 10 }}, color: '#555' }},
      }},
    }},
  }}
}});

// ── Monthly overview chart ────────────────────────────────────────────────────
(function() {{
  const monthly = DATA.monthly;
  if (!monthly || !monthly.months.length) return;
  const monthlyCard = document.createElement('div');
  monthlyCard.className = 'card';
  monthlyCard.innerHTML = '<h3>Monthly Cases (Trailing 10 Years)</h3><canvas id="cMonthly" style="max-height:220px"></canvas>' +
    '<div class="stats"><span style="color:#aaa;font-size:10px">stacked bars = % by status &nbsp;·&nbsp; line = total cases</span></div>';
  document.getElementById('monthlyWrap').appendChild(monthlyCard);
  const mLabels = monthly.months.map(function(m) {{
    const p = m.split('-');
    return new Date(+p[0], +p[1] - 1, 1).toLocaleDateString('en-US', {{ month: 'short', year: 'numeric' }});
  }});
  const pct = function(arr, i) {{ return monthly.total[i] ? +(arr[i] / monthly.total[i] * 100).toFixed(1) : 0; }};
  new Chart(document.getElementById('cMonthly'), {{
    type: 'bar',
    data: {{
      labels: mLabels,
      datasets: [
        {{ label: 'Clear',   data: monthly.months.map(function(_,i){{ return pct(monthly.clear,i);   }}), backgroundColor: '#8FC9A0', stack: 'stack', order: 2, pointStyle: 'rect', yAxisID: 'yPct' }},
        {{ label: 'Reject',  data: monthly.months.map(function(_,i){{ return pct(monthly.reject,i);  }}), backgroundColor: '#E89E98', stack: 'stack', order: 2, pointStyle: 'rect', yAxisID: 'yPct' }},
        {{ label: 'Pending', data: monthly.months.map(function(_,i){{ return pct(monthly.pending,i); }}), backgroundColor: '#BDD0E4', stack: 'stack', order: 2, pointStyle: 'rect', yAxisID: 'yPct' }},
        {{ type: 'line', label: 'Total Cases', data: monthly.total, borderColor: '#1e293b', backgroundColor: 'transparent', borderWidth: 1.5, pointRadius: 2, pointHoverRadius: 5, tension: 0.3, fill: false, order: 1, yAxisID: 'yTotal' }},
      ],
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{
        legend: {{ position: 'top', labels: {{ font: {{ size: 10 }}, padding: 10, usePointStyle: true, boxWidth: 8 }} }},
        tooltip: {{
          mode: 'index', intersect: false,
          callbacks: {{
            label: function(ctx) {{
              if (ctx.dataset.type === 'line') return 'Total: ' + ctx.parsed.y;
              const i = ctx.dataIndex;
              const abs = {{ 'Clear': monthly.clear[i], 'Reject': monthly.reject[i], 'Pending': monthly.pending[i] }}[ctx.dataset.label] ?? '';
              return ctx.dataset.label + ': ' + ctx.parsed.y + '% (' + abs + ')';
            }},
          }},
        }},
        annotation: {{ annotations: {{
          covid: {{
            type: 'line', scaleID: 'x',
            value: mLabels.findIndex(function(l) {{ return l.includes('Jan') && l.includes('2020'); }}),
            borderColor: 'rgba(180,0,0,0.35)', borderWidth: 1,
            borderDash: [4, 4],
            label: {{ content: '🦠 COVID-19', display: true, position: 'end',
              font: {{ size: 9 }}, color: 'rgba(150,0,0,0.5)',
              backgroundColor: 'rgba(255,255,255,0.85)', padding: {{ x:4, y:2 }}, borderRadius: 2 }},
          }},
        }} }},
      }},
      scales: {{
        x: {{ stacked: true,
          ticks: {{ font: {{ size: 9 }}, color: '#aaa', maxRotation: 45,
            callback: function(val, i) {{
              const l = mLabels[i] || '';
              return l.startsWith('Jan') ? l : '';
            }}
          }},
          grid: {{ display: false }} }},
        yPct: {{ type: 'linear', position: 'left', stacked: true, min: 0, max: 100,
          ticks: {{ font: {{ size: 9 }}, color: '#aaa', callback: function(v) {{ return v + '%'; }} }},
          grid: {{ color: 'rgba(0,0,0,0.05)' }},
          afterFit: function(axis) {{ axis.width = 42; }} }},
        yTotal: {{ type: 'linear', position: 'right', beginAtZero: true,
          ticks: {{ font: {{ size: 9 }}, color: '#aaa' }},
          grid: {{ drawOnChartArea: false }},
          afterFit: function(axis) {{ axis.width = 38; }} }},
      }},
    }},
  }});

  // ── Avg Waiting Days chart ────────────────────────────────────────────────
  const avgWait = monthly.avg_wait || [];
  if (avgWait.some(function(v) {{ return v !== null; }})) {{
    const waitCard2 = document.createElement('div');
    waitCard2.className = 'card';
    waitCard2.innerHTML = '<h3>Avg Waiting Days for Completed Cases (Trailing 10 Years)</h3><canvas id="cAvgWait" style="max-height:220px"></canvas>' +
      '<div class="stats"><span style="color:#aaa;font-size:10px">line = avg waiting days for cleared/rejected cases that month</span></div>';
    document.getElementById('waitWrap').appendChild(waitCard2);
    new Chart(document.getElementById('cAvgWait'), {{
      type: 'line',
      data: {{
        labels: mLabels,
        datasets: [{{
          label: 'Avg Waiting Days',
          data: avgWait,
          borderColor: '#1e293b',
          backgroundColor: 'transparent',
          borderWidth: 1.5,
          pointRadius: 2,
          pointHoverRadius: 5,
          tension: 0.3,
          fill: false,
          spanGaps: true,
        }}],
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{ mode: 'index', intersect: false, callbacks: {{
            label: function(ctx) {{ return 'Avg wait: ' + (ctx.parsed.y !== null ? ctx.parsed.y + 'd' : 'N/A'); }},
          }} }},
          annotation: {{ annotations: {{
            covid: {{
              type: 'line', scaleID: 'x',
              value: mLabels.findIndex(function(l) {{ return l.includes('Jan') && l.includes('2020'); }}),
              borderColor: 'rgba(180,0,0,0.3)', borderWidth: 1,
              borderDash: [4, 4],
              label: {{ content: '🦠 COVID-19', display: true, position: 'start',
                font: {{ size: 9 }}, color: 'rgba(150,0,0,0.5)',
                backgroundColor: 'rgba(255,255,255,0.85)', padding: {{ x:4, y:2 }}, borderRadius: 2 }},
            }},
            obama: {{
              type: 'box', xScaleID: 'x',
              xMin: 0,
              xMax: mLabels.findIndex(function(l) {{ return l.includes('Jan') && l.includes('2017'); }}),
              backgroundColor: 'rgba(33,150,243,0.06)', borderWidth: 0,
              label: {{ content: 'Obama', display: true, position: {{ x: 'center', y: 'start' }},
                font: {{ size: 10, style: 'italic' }}, color: 'rgba(33,150,243,0.5)' }},
            }},
            trump1: {{
              type: 'box', xScaleID: 'x',
              xMin: mLabels.findIndex(function(l) {{ return l.includes('Jan') && l.includes('2017'); }}),
              xMax: mLabels.findIndex(function(l) {{ return l.includes('Jan') && l.includes('2021'); }}),
              backgroundColor: 'rgba(244,67,54,0.06)', borderWidth: 0,
              label: {{ content: 'Trump I', display: true, position: {{ x: 'center', y: 'start' }},
                font: {{ size: 10, style: 'italic' }}, color: 'rgba(244,67,54,0.5)' }},
            }},
            biden: {{
              type: 'box', xScaleID: 'x',
              xMin: mLabels.findIndex(function(l) {{ return l.includes('Jan') && l.includes('2021'); }}),
              xMax: mLabels.findIndex(function(l) {{ return l.includes('Jan') && l.includes('2025'); }}),
              backgroundColor: 'rgba(33,150,243,0.06)', borderWidth: 0,
              label: {{ content: 'Biden', display: true, position: {{ x: 'center', y: 'start' }},
                font: {{ size: 10, style: 'italic' }}, color: 'rgba(33,150,243,0.5)' }},
            }},
            trump2: {{
              type: 'box', xScaleID: 'x',
              xMin: mLabels.findIndex(function(l) {{ return l.includes('Jan') && l.includes('2025'); }}),
              xMax: mLabels.length - 1,
              backgroundColor: 'rgba(244,67,54,0.06)', borderWidth: 0,
              label: {{ content: 'Trump II', display: true, position: {{ x: 'center', y: 'start' }},
                font: {{ size: 10, style: 'italic' }}, color: 'rgba(244,67,54,0.5)' }},
            }},
          }} }},
        }},
        scales: {{
          x: {{
            ticks: {{ font: {{ size: 9 }}, color: '#aaa',
              callback: function(val, i) {{
                const l = mLabels[i] || '';
                return (l.startsWith('Jan') || l.startsWith('May') || l.startsWith('Sep')) ? l : '';
              }}
            }},
            grid: {{ color: 'rgba(0,0,0,0.04)', drawTicks: false }},
          }},
          y: {{ beginAtZero: true,
            ticks: {{ font: {{ size: 9 }}, color: '#aaa', callback: function(v) {{ return v + 'd'; }} }},
            grid: {{ color: 'rgba(0,0,0,0.05)' }},
            afterFit: function(axis) {{ axis.width = 42; }} }},
          yRight: {{ type: 'linear', position: 'right', display: false,
            afterFit: function(axis) {{ axis.width = 38; }} }},
        }},
      }},
    }});
  }}
}})();

// ── Records table ─────────────────────────────────────────────────────────
let tableSortCol = 2;   // default: complete date (col index 2 in cols array)
let tableSortDir = -1;  // -1 = desc (newest first)

function renderTable(records) {{
  // cols: [label, record-index, css-class, width]
  const cols = [
    ['Status',        3, '',           '62px'],
    ['Check Date',    4, '',           '92px'],
    ['Complete Date', 0, '',           '110px'],
    ['Waiting Days',  2, 'col-days',   '76px'],
    ['Visa Type',     1, 'col-center',  '72px'],
    ['Entry',         6, '',           '65px'],
    ['Consulate',     5, '',           '90px'],
    ['Major',         7, 'col-major',   '140px'],
    ['Details',       8, 'col-details',''],
  ];

  // Build header once
  const thead = document.querySelector('#recordsTable thead');
  if (!thead.children.length) {{
    const tr = document.createElement('tr');
    const thNum = document.createElement('th');
    thNum.textContent = '#';
    thNum.style.cssText = 'width:36px;text-align:right;color:#aaa';
    tr.appendChild(thNum);
    cols.forEach(([label, , cls, w], ci) => {{
      const th = document.createElement('th');
      th.textContent = label;
      if (cls) th.className = cls;
      if (w) th.style.width = w;
      th.dataset.ci = ci;
      th.addEventListener('click', () => {{
        if (tableSortCol === ci) {{ tableSortDir *= -1; }}
        else {{ tableSortCol = ci; tableSortDir = 1; }}
        renderTable(_currentTableRecords);
      }});
      tr.appendChild(th);
    }});
    thead.appendChild(tr);
  }}

  // Sort indicators (ci-1 because th[0] is the non-sortable # column)
  thead.querySelectorAll('th').forEach((th, ci) => {{
    th.classList.remove('table-sort-asc', 'table-sort-desc');
    if (ci - 1 === tableSortCol)
      th.classList.add(tableSortDir === 1 ? 'table-sort-asc' : 'table-sort-desc');
  }});

  // Sort records
  const [, idx] = cols[tableSortCol];
  const sorted = [...records].sort((a, b) => {{
    const av = a[idx], bv = b[idx];
    return tableSortDir * (av < bv ? -1 : av > bv ? 1 : 0);
  }});

  // Render tbody
  const tbody = document.querySelector('#recordsTable tbody');
  tbody.innerHTML = '';
  sorted.forEach((r, i) => {{
    const tr = document.createElement('tr');
    const tdNum = document.createElement('td');
    tdNum.textContent = i + 1;
    tdNum.style.cssText = 'text-align:right;color:#aaa;user-select:none';
    tr.appendChild(tdNum);
    cols.forEach(([, ri, cls]) => {{
      const td = document.createElement('td');
      if (cls) td.className = cls;
      td.textContent = r[ri] ?? '';
      tr.appendChild(td);
    }});
    tbody.appendChild(tr);
  }});

  // Count line
  const countEl = document.getElementById('tableCount');
  if (countEl) countEl.textContent = sorted.length + ' record' + (sorted.length !== 1 ? 's' : '');
}}

let _currentTableRecords = DATA.raw_records;
renderFilters();
updateAllCharts(DATA.raw_records);
</script>
</body>
</html>"""


if __name__ == "__main__":
    import sys
    ci = "--ci" in sys.argv or os.environ.get("CI") == "true"
    print(f"Scraping checkee.info... (ci={ci})")
    try:
        records = scrape(ci=ci)
    except Exception as e:
        print(f"ERROR: scrape failed after all retries: {e}")
        print("Keeping existing index.html unchanged.")
        sys.exit(0)
    print(f"Found {len(records)} records")
    if not records:
        print("WARNING: 0 records returned (site may be blocking CI). Keeping existing index.html.")
        sys.exit(0)
    print("Scraping monthly case table...")
    try:
        monthly = scrape_monthly(ci=ci)
        print(f"Monthly rows: {len(monthly['months'])}")
    except Exception as e:
        print(f"WARNING: monthly scrape failed: {e}. Using empty data.")
        monthly = {"months": [], "pending": [], "clear": [], "reject": [], "total": []}
    data = build_data(records, monthly)
    print(f"Dates: {data['dates'][0] if data['dates'] else 'none'} → {data['dates'][-1] if data['dates'] else 'none'}")
    print(f"Statuses found: {data['complete_dist']['statuses']}")
    updated = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M CST")
    html = generate_html(data, updated)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Generated index.html ✓")
