"""
ICAI batch watcher (cloud version, runs on GitHub Actions)

Checks Southern > HYDERABAD > Advanced (ICITSS) MCS Course every ~15 seconds
and sends Telegram alerts when:
  - a batch NOT in KNOWN_BATCHES appears, or
  - any batch shows Available Seats > 0

Reads settings from environment variables (set as GitHub Secrets):
  TG_TOKEN, TG_CHAT_ID   (required)
  RUN_MINUTES            how long this run should keep checking (default 345)
  KNOWN_BATCHES          comma-separated batch numbers already known/closed
"""
import datetime
import os
import random
import sys
import time

import requests
from playwright.sync_api import sync_playwright

URL = "https://www.icaionlineregistration.org/launchbatchdetail.aspx"
LOGIN_URL = "https://www.icaionlineregistration.org/"
REGION = "Southern"
POU = "HYDERABAD"
COURSE_CONTAINS = "MCS"
POLL_SECONDS = 15
REALERT_SECONDS = 20

TG_TOKEN = os.getenv("TG_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
RUN_MINUTES = float(os.getenv("RUN_MINUTES", "345"))
KNOWN = {
    x.strip()
    for x in os.getenv(
        "KNOWN_BATCHES",
        "AdvICITSSMCS__HYDERABAD_133,AdvICITSSMCS__HYDERABAD_134",
    ).split(",")
    if x.strip()
}


def now():
    return datetime.datetime.utcnow().strftime("%H:%M:%S UTC")


def notify(msg, silent=False):
    print(f"[{now()}] {msg}", flush=True)
    if not (TG_TOKEN and TG_CHAT_ID):
        print("Telegram secrets missing!", flush=True)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={
                "chat_id": TG_CHAT_ID,
                "text": msg,
                "disable_notification": "true" if silent else "false",
            },
            timeout=15,
        )
    except Exception as e:
        print(f"[{now()}] Telegram send failed: {e}", flush=True)


def pick(page, index, contains):
    sel = page.locator("select").nth(index)
    options = [o.strip() for o in sel.locator("option").all_text_contents()]
    for i, text in enumerate(options):
        if contains.lower() in text.lower():
            sel.select_option(index=i)
            page.wait_for_load_state("networkidle")
            time.sleep(0.5)
            return
    raise RuntimeError(f"Option containing '{contains}' not found in {options}")


def fetch_batches(browser):
    page = browser.new_page()
    try:
        page.goto(URL, wait_until="networkidle", timeout=40000)
        pick(page, 0, REGION)
        pick(page, 1, POU)
        pick(page, 2, COURSE_CONTAINS)
        page.locator("input[value='Get List'], button:has-text('Get List')").first.click()
        page.wait_for_load_state("networkidle")
        time.sleep(0.5)

        batches = []
        for tr in page.locator("tr").all():
            cells = [c.strip() for c in tr.locator("xpath=./td").all_text_contents()]
            if len(cells) >= 8 and cells[1].isdigit():
                batches.append(
                    {
                        "batch": cells[0],
                        "seats": int(cells[1]),
                        "from": cells[2],
                        "to": cells[3],
                        "time": " ".join(cells[4].split()),
                    }
                )
        return batches
    finally:
        page.close()


def main():
    if not (TG_TOKEN and TG_CHAT_ID):
        print("ERROR: TG_TOKEN / TG_CHAT_ID secrets are not set.")
        sys.exit(1)

    end_time = time.time() + RUN_MINUTES * 60
    alerted_new = set()
    last_seat_alert = {}
    failures = 0
    ever_ok = False
    warned_blocked = False
    checks = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        print(f"[{now()}] Cloud watcher run started, will run {RUN_MINUTES} min.", flush=True)

        while time.time() < end_time:
            try:
                batches = fetch_batches(browser)
                failures = 0
                checks += 1

                if not ever_ok:
                    ever_ok = True
                    summary = "\n".join(
                        f"{b['batch']} seats={b['seats']} {b['from']} to {b['to']} {b['time']}"
                        for b in batches
                    ) or "No batches listed."
                    notify(f"Cloud watcher is running OK (silent message).\n{summary}", silent=True)

                for b in batches:
                    key = b["batch"]
                    detail = (
                        f"{key}\nseats={b['seats']}\n{b['from']} to {b['to']}\n{b['time']}\n"
                        f"GO NOW: {LOGIN_URL}"
                    )
                    if key not in KNOWN and key not in alerted_new:
                        alerted_new.add(key)
                        notify(f"NEW BATCH RELEASED!\n{detail}")
                        last_seat_alert[key] = time.time()
                    elif b["seats"] > 0 and time.time() - last_seat_alert.get(key, 0) > REALERT_SECONDS:
                        notify(f"SEATS OPEN!\n{detail}")
                        last_seat_alert[key] = time.time()

                if checks % 20 == 0:
                    print(f"[{now()}] {checks} checks done, {len(batches)} batch(es).", flush=True)

            except Exception as e:
                failures += 1
                print(f"[{now()}] error ({failures}): {e}", flush=True)
                # restart the browser in case it got stuck
                try:
                    browser.close()
                except Exception:
                    pass
                try:
                    browser = p.chromium.launch(headless=True)
                except Exception as e2:
                    print(f"[{now()}] browser relaunch failed: {e2}", flush=True)
                if failures >= 10 and not warned_blocked:
                    warned_blocked = True
                    msg = (
                        "Cloud watcher cannot reach/read the ICAI page "
                        + ("(it worked earlier, so ICAI may be down)." if ever_ok else
                           "from GitHub's servers. ICAI may be blocking them. Keep using your laptop watcher.")
                    )
                    notify(msg, silent=False)

            time.sleep(POLL_SECONDS + random.uniform(0, 4))

        try:
            browser.close()
        except Exception:
            pass
    print(f"[{now()}] Run finished normally after {checks} checks. Next run takes over.", flush=True)


if __name__ == "__main__":
    main()
