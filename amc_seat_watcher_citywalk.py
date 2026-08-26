#!/usr/bin/env python3
"""
amc_seat_watcher_citywalk.py  (STANDALONE - CityWalk / Los Angeles)
-------------------------------------------------------------------
Watches AMC IMAX 70mm showtimes for "The Odyssey" at AMC Universal Cinema
(CityWalk, Los Angeles) and pushes a phone alert (via ntfy) the moment a seat
frees up. Runs INDEPENDENTLY of the NYC watcher; publishes to ONE channel.

Checks showtimes concurrently (a pool of browser tabs) and alerts the instant
any single show shows an opening. NOTIFY-ONLY: it tells a human "go grab seat
H14"; it never logs in, carts, holds, or buys anything.

CHANNEL (subscribe in the ntfy app)
  odyssey_citywalk_all

SETUP (one time)
  pip install playwright requests
  playwright install chromium
  (token: put your ntfy token in ntfy_token.txt next to this file - same one the
   NYC watcher uses is fine; publishes go out authenticated.)
RUN
  python amc_seat_watcher_citywalk.py    (keep awake: caffeinate -dimsu python ...)
  Ctrl+C to stop.
"""

import re
import os
import time
import random
import asyncio
import datetime as dt
from zoneinfo import ZoneInfo
import requests
from playwright.async_api import async_playwright

# ---- ntfy publish token (keeps your quota + lets you lock topics) -------------

# NTFY_TOKEN environment variable or a local file "ntfy_token.txt" (one line, the
# tk_... token) sitting next to this script.
def _extract_token(raw):
    """Pull just the tk_... token out of whatever we're given (handles TextEdit
    RTF, stray whitespace, newlines, quotes)."""
    if not raw:
        return ""
    m = re.search(r'tk_[A-Za-z0-9]+', raw)
    return m.group(0) if m else ""

def _load_ntfy_token():
    tok = _extract_token(os.environ.get("NTFY_TOKEN", ""))
    if tok:
        return tok
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "ntfy_token.txt"), encoding="utf-8", errors="ignore") as f:
            return _extract_token(f.read())
    except Exception:
        return ""

NTFY_TOKEN = _load_ntfy_token()

# ============================ CONFIG ============================
# STANDALONE watcher for AMC Universal Cinema at CityWalk (Los Angeles).
# Single ntfy channel: odyssey_citywalk_all.

# Discovery uses this theatre's showtimes page. If discovery ever reports 0
# candidates, this URL is the thing to check/fix.
LISTING_URL = ("https://www.amctheatres.com/movie-theatres/los-angeles/"
               "universal-cinema-an-amc-theatre/showtimes?date={date}")
THEATRE_LABEL = "AMC Universal Cinema (CityWalk)"
THEATRE_TZ = "America/Los_Angeles"   # LA time, so alerts show correct local time

# The one and only channel for this venue.
THE_TOPIC = "odyssey_citywalk_all"

# Date window (inclusive). START_DATE = None means today.
START_DATE = None
END_DATE = "2026-10-20"

# Format tags: discovery casts a wide net; the strict VERIFY_TAG is the real gate
# checked on each seat page, so ONLY true IMAX 70mm shows ever alert.
DISCOVERY_TAG = "70mm"
VERIFY_TAG = "imax70mm"

# Title gate. A show must ALSO be this movie, so if CityWalk ever puts another
# film on the IMAX 70mm screen it won't ping your subscribers. "" turns it off.
TITLE_MUST_CONTAIN = "odyssey"

# How many showtimes to check at once. Higher = faster sweeps, more load on AMC.
CONCURRENCY = 3

MANUAL_SHOWTIMES = set()            # optionally pin extra showtime IDs by hand

FRONT_ROWS_TO_SKIP = {"A", "B", "C", "D", "E", "F"}  # front rows: found, logged, never alerted
WANTED_TYPES = {"CanReserve"}       # normal seats; skips wheelchair/companion

POLL_SECONDS = 45                   # pause between sweeps (sweep itself is ~1 min)
REDISCOVER_SECONDS = 300            # re-scan which shows exist every 15 min
ALERT_COOLDOWN_SECONDS = 1800      # changes-only-ish: NEW openings fire instantly;
                                    # a still-open seat re-pings at most every 6h

# Only alert when a show has at most this many open seats (the cancellation
# signal). Keeps alert volume under the free ntfy daily quota. 0 = no cap
# (only do that with a registered/paid ntfy account).
MAX_OPEN_TO_ALERT = 50

SEAT_NAME_LIMIT = 500               # list every seat name (a full house is ~480)

HEADFUL = False

# ================================================================

SEAT_RE = re.compile(
    r'\{"available":(true|false),"column":(\d+),"row":(\d+),'
    r'"name":"([^"]*)","type":"([^"]*)","seatTier":"([^"]*)",'
    r'"shouldDisplay":(true|false)\}'
)
#SHOWTIME_ID_RE = re.compile(r'/showtimes/(\d+)')
SHOWTIME_ID_RE = re.compile(r'(?:/showtimes/|"showtimeId":)(\d+)')


def date_range():
    start = dt.date.fromisoformat(START_DATE) if START_DATE else dt.date.today()
    end = dt.date.fromisoformat(END_DATE)
    return [(start + dt.timedelta(days=i)).isoformat()
            for i in range((end - start).days + 1)]


def pretty_day(date_str):
    return dt.date.fromisoformat(date_str).strftime("%a, %b %d").replace(" 0", " ")


def fmt_seats(seats):
    if len(seats) <= SEAT_NAME_LIMIT:
        return ", ".join(seats)
    return f"{len(seats)} seats (e.g. {', '.join(seats[:SEAT_NAME_LIMIT])}, ...)"


def showtime_label(html, showtime_id, date_str):
    """'<listing day> - <clock time>', e.g. 'Sat, Aug 1 - 2:00 AM EDT'. Day comes
    from the listing date (matches the topic); time from AMC's UTC 'when' field."""
    day = pretty_day(date_str)
    m = re.search(r'"when":"(20\d\d-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)"', html)
    if not m:
        m = re.search(r'"showDateTimeUtc":"(20\d\d-\d\d-\d\dT\d\d:\d\d:\d\d)', html)
    if not m:
        return day
    iso = m.group(1)
    if not iso.endswith("Z"):
        iso += "Z"
    utc = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    try:
        local = utc.astimezone(ZoneInfo(THEATRE_TZ))
    except Exception:
        off = re.search(r'"utcOffset":"([+-]\d\d):(\d\d)"', html)
        if off:
            delta = dt.timedelta(hours=int(off.group(1)),
                                 minutes=int(off.group(1)[0] + off.group(2)))
            local = utc.astimezone(dt.timezone(delta))
        else:
            return day
    tm = local.strftime("%I:%M %p %Z")
    if tm.startswith("0"):
        tm = tm[1:]
    return f"{day} - {tm}"


def parse_seats(html):
    """Return (open_wanted_names_sorted, other_available_labels)."""
    open_seats, other = [], []
    for m in SEAT_RE.finditer(html):
        available = m.group(1) == "true"
        name = m.group(4)
        stype = m.group(5)
        should = m.group(7) == "true"
        if not available or not should:
            continue
        row_skipped = name[:1].upper() in FRONT_ROWS_TO_SKIP
        if stype in WANTED_TYPES and not row_skipped:
            open_seats.append(name)
        else:
            other.append(f"{name}({stype})")

    def sort_key(n):
        return (n[:1], int(re.sub(r"\D", "", n) or 0))

    return sorted(set(open_seats), key=sort_key), other


def notify(topics, title, body, click_url):
    """Publish to each ntfy topic. Returns number of topics that accepted it."""
    safe_title = title.encode("latin-1", "ignore").decode("latin-1")
    headers = {"Title": safe_title, "Priority": "high",
               "Tags": "clapper,ticket", "Click": click_url}
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"   # publish as your account
    ok = 0
    for topic in topics:
        try:
            r = requests.post(
                f"https://ntfy.sh/{topic}",
                data=body.encode("utf-8"),
                headers=headers,
                timeout=15,
            )
            if r.status_code == 429:
                print(f"[warn] ntfy RATE-LIMITED on {topic} (429) -> DROPPED. "
                      f"Too many alerts; free daily quota exhausted.")
            elif not r.ok:
                print(f"[warn] ntfy {topic} HTTP {r.status_code}: {r.text[:100]}")
            else:
                ok += 1
        except Exception as e:
            print(f"[warn] ntfy push to {topic} failed: {e}")
    print("\a", end="", flush=True)
    return ok


# ------------------------ browser helpers ------------------------

async def _block_assets(route):
    if route.request.resource_type in ("image", "media", "font", "stylesheet"):
        await route.abort()
    else:
        await route.continue_()


async def fetch_html(context, url, sem, wait_ms=1500):
    """Load a URL in a fresh tab (concurrency-limited) and return its HTML."""
    async with sem:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(wait_ms)
            return (await page.content()).replace('\\"', '"')
        finally:
            await page.close()


async def fetch_listing(context, date_str, sem):
    """Return {showtime_id: date_str} for candidate shows on one listing day."""
    url = LISTING_URL.format(date=date_str)
    try:
        html = await fetch_html(context, url, sem, wait_ms=1200)
    except Exception as e:
        print(f"[warn] discovery {date_str}: {e}")
        return {}
    found = {}
    for m in re.finditer(re.escape(DISCOVERY_TAG), html, re.IGNORECASE):
        window = html[max(0, m.start() - 1200): m.start() + 1200]
        for sid in SHOWTIME_ID_RE.findall(window):
            found.setdefault(sid, date_str)
    return found


async def discover(context, sem):
    results = await asyncio.gather(
        *(fetch_listing(context, d, sem) for d in date_range()))
    found = {}
    for chunk in results:
        for sid, ds in chunk.items():
            found.setdefault(sid, ds)
    for sid in MANUAL_SHOWTIMES:
        found.setdefault(sid, "manual")
    return found


async def check_show(context, sid, date_str, sem):
    """Returns (sid, date_str, is_imax70, open_seats, other_available, url, label)."""
    url = f"https://www.amctheatres.com/showtimes/{sid}/seats"
    buster = f"{url}?_={int(time.time() * 1000)}"
    try:
        html = await fetch_html(context, buster, sem, wait_ms=1500)
    except Exception as e:
        print(f"[warn] {sid}: {e}")
        return (sid, date_str, None, [], [], url, f"show {sid}")
    low = html.lower()
    is_imax70 = (VERIFY_TAG.lower() in low
                 and (not TITLE_MUST_CONTAIN
                      or TITLE_MUST_CONTAIN.lower() in low))
    label = showtime_label(html, sid, date_str)
    open_seats, other = parse_seats(html)
    return (sid, date_str, is_imax70, open_seats, other, url, label)


# ------------------------------ main ------------------------------

async def run():
    window = date_range()
    print(f"Watching {THEATRE_LABEL} for IMAX 70mm shows ({CONCURRENCY} tabs)")
    print("Publishing: " + ("AUTHENTICATED (your ntfy account, higher quota)"
                            if NTFY_TOKEN else
                            "ANONYMOUS (low free quota - add a token to raise it)"))
    print(f"Date window: {window[0]} through {window[-1]}")
    print(f"Channel (subscribe in ntfy app): {THE_TOPIC}\n")

    # Per date: {seat_name: last_time_we_alerted_it}. A seat only re-alerts if
    # it's brand new or its last alert was more than ALERT_COOLDOWN_SECONDS ago.
    # This kills flicker-spam (seats blinking in/out during checkout holds) that
    # otherwise fires an alert every sweep and drains the ntfy quota.
    alerted_at = {}
    shows = {}
    last_discovery = 0.0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not HEADFUL)
        context = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"))
        await context.set_extra_http_headers(
            {"Cache-Control": "no-cache", "Pragma": "no-cache"})
        await context.route("**/*", _block_assets)
        sem = asyncio.Semaphore(CONCURRENCY)

        while True:
            if time.time() - last_discovery > REDISCOVER_SECONDS:
                shows = await discover(context, sem)
                last_discovery = time.time()
                print(f"[{time.strftime('%H:%M:%S')}] discovered {len(shows)} "
                      f"candidate showtime(s)")

            # Kick off all checks concurrently, but handle each show THE MOMENT
            # it finishes (as_completed) and alert immediately -- don't wait for
            # the rest of the batch. The tabs still run in parallel; we just react
            # per-show instead of per-sweep.
            sent_this_sweep = 0
            tasks = [check_show(context, sid, ds, sem)
                     for sid, ds in shows.items()]
            for fut in asyncio.as_completed(tasks):
                sid, date_str, is_imax70, open_list, other, url, label = await fut
                stamp = time.strftime("%H:%M:%S")
                if is_imax70 is None:
                    continue  # load failed; already warned
                if not is_imax70:
                    print(f"[{stamp}] show {sid}: skipped "
                          f"(not Odyssey IMAX 70mm)")
                    continue
                n = len(open_list)
                if n == 0:
                    note = ""
                    if other:
                        note = (f"  [{len(other)} available but filtered: "
                                f"{', '.join(other[:8])}]")
                    print(f"[{stamp}] {label}: sold out{note}")
                    continue
                if MAX_OPEN_TO_ALERT and n > MAX_OPEN_TO_ALERT:
                    print(f"[{stamp}] {label}: {n} open (above cap; skipped)")
                    continue

                print(f"[{stamp}] {label}: OPEN -> {', '.join(open_list)}")

                # Per-show, per-seat de-dup: only alert on seats that are new (or
                # went stale past the cooldown), so a flickering seat can't spam.
                now = time.time()
                seen = alerted_at.setdefault(sid, {})
                fresh = [s for s in open_list
                         if s not in seen or now - seen[s] > ALERT_COOLDOWN_SECONDS]
                if not fresh:
                    continue  # already told people about these seats

                # Fire this show's alert immediately to the single venue topic.
                sent = await asyncio.to_thread(
                    notify, [THE_TOPIC],
                    label, f"{fmt_seats(open_list)}\n{url}", url)
                if sent:
                    sent_this_sweep += sent
                    print(f"  -> alert sent: {THE_TOPIC}")
                else:
                    print("  -> alert NOT delivered (see warnings above)")
                for s in open_list:
                    seen[s] = now

            if sent_this_sweep:
                print(f"[{time.strftime('%H:%M:%S')}] sweep sent "
                      f"{sent_this_sweep} ntfy message(s)")

            await asyncio.sleep(max(15, POLL_SECONDS + random.uniform(-6, 6)))


if __name__ == "__main__":
    asyncio.run(run())
