#!/usr/bin/env python3
"""Scrape checkee.info month-by-month from a given start month to today,
then regenerate index.html with all records merged."""

import time
import re
from datetime import datetime, timezone
from collections import defaultdict

from generate import (
    build_chrome_options,
    parse_rows,
    wait_for_dispdate_selects,
    wait_for_records_after_submit,
    save_debug_html,
    describe_table_rows,
    build_data,
    generate_html,
    scrape_monthly,
)


def scrape_month(driver, month_str, By, SeleniumSelect):
    """Submit the monthly form for a given YYYY-MM and return parsed records."""
    from selenium.webdriver.common.by import By

    driver.get("https://www.checkee.info/main.php?sortby=clear_date")
    selects = wait_for_dispdate_selects(driver, By)

    # First dropdown = monthly view (YYYY-MM)
    sel_monthly = SeleniumSelect(selects[0])
    target_val = None
    for opt in sel_monthly.options:
        if opt.get_attribute("value") == month_str:
            target_val = month_str
            sel_monthly.select_by_value(month_str)
            break

    if not target_val:
        available = [o.get_attribute("value") for o in sel_monthly.options]
        print(f"  WARNING: month {month_str} not found. Available: {available[:5]}...")
        return []

    # Submit the first form (monthly)
    forms = driver.find_elements(By.XPATH, "//form[@action='./disppage.php']")
    if len(forms) >= 1:
        forms[0].submit()
    else:
        selects[0].find_element(By.XPATH, "./ancestor::form").submit()

    print(f"  Landed on: {driver.current_url}")
    records, page_source, soup = wait_for_records_after_submit(driver)
    if not records:
        save_debug_html(f"debug-{month_str}.html", page_source)
        diag = describe_table_rows(soup)
        print(f"  Row cell counts: {diag['cell_counts']}")
    return records


def _make_driver(ci=False):
    """Create a Chrome driver — undetected_chromedriver in CI, regular Selenium locally."""
    from selenium.webdriver.common.by import By
    if ci:
        import undetected_chromedriver as uc
        opts = build_chrome_options(ci=True)
        return uc.Chrome(options=opts, headless=True, use_subprocess=False)
    else:
        from selenium import webdriver
        opts = build_chrome_options(force_refresh=True)
        return webdriver.Chrome(options=opts)


def main():
    import os
    import sys
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select as SeleniumSelect

    ci = "--ci" in sys.argv or os.environ.get("CI") == "true"
    print(f"scrape_all starting (ci={ci})")

    # Determine month range: 2026-01 to current month
    now = datetime.now(timezone.utc)
    start_year, start_month = 2026, 1
    end_year, end_month = now.year, now.month

    months = []
    y, m = start_year, start_month
    while (y, m) <= (end_year, end_month):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1

    print(f"Will scrape {len(months)} months: {months[0]} to {months[-1]}")

    all_records = []
    seen_keys = set()

    driver = _make_driver(ci=ci)
    try:
        for i, month_str in enumerate(months):
            print(f"\n[{i+1}/{len(months)}] Scraping {month_str}...")
            try:
                records = scrape_month(driver, month_str, By, SeleniumSelect)
                print(f"  Got {len(records)} records")

                # Deduplicate
                for r in records:
                    key = (r["date"], r["visa"], r["days"], r["check_date"])
                    if key not in seen_keys:
                        seen_keys.add(key)
                        all_records.append(r)

                time.sleep(3)  # Be polite between requests
            except Exception as e:
                print(f"  ERROR scraping {month_str}: {e}")
                time.sleep(5)
    finally:
        driver.quit()

    # Also try to get 90-day data to capture any records not in monthly views
    print("\n--- Scraping 90-day view for completeness ---")
    driver2 = _make_driver(ci=ci)
    try:
        driver2.get("https://www.checkee.info/main.php?sortby=clear_date")
        selects = wait_for_dispdate_selects(driver2, By)
        sel90 = SeleniumSelect(selects[1])
        for opt in sel90.options:
            if "90" in opt.text:
                sel90.select_by_value(opt.get_attribute("value"))
                break
        forms = driver2.find_elements(By.XPATH, "//form[@action='./disppage.php']")
        if len(forms) >= 2:
            forms[1].submit()
        else:
            selects[1].find_element(By.XPATH, "./ancestor::form").submit()
        records90, _, _ = wait_for_records_after_submit(driver2)
        print(f"90-day view: {len(records90)} records")
        for r in records90:
            key = (r["date"], r["visa"], r["days"], r["check_date"])
            if key not in seen_keys:
                seen_keys.add(key)
                all_records.append(r)
    except Exception as e:
        print(f"90-day scrape error: {e}")
    finally:
        driver2.quit()

    # Filter to Jan 1 onwards
    cutoff = "2026-01-01"
    all_records = [r for r in all_records if r["date"] >= cutoff]
    all_records.sort(key=lambda r: r["date"])

    print(f"\n=== Total unique records since {cutoff}: {len(all_records)} ===")
    if all_records:
        dates = sorted(set(r["date"] for r in all_records))
        print(f"Date range: {dates[0]} to {dates[-1]}")
        vc = defaultdict(int)
        for r in all_records:
            vc[r["visa"]] += 1
        print(f"Visa types: {dict(sorted(vc.items()))}")

    # Get monthly summary data
    print("\nScraping monthly summary...")
    monthly = scrape_monthly(ci=ci)

    # Build data and generate HTML
    data = build_data(all_records, monthly)
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    date_label = f"Since {cutoff}"
    html = generate_html(data, updated, date_label=date_label)

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nGenerated index.html ({len(html):,} bytes) ✓")


if __name__ == "__main__":
    main()
