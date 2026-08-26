#!/usr/bin/env python3
"""
amc_seat_watcher.py  (PARALLEL build)
-------------------------------------
Watches AMC IMAX 70mm showtimes for "The Odyssey" at AMC Lincoln Square 13 and
broadcasts a phone alert (via ntfy) the moment a seat frees up.

This build checks many showtimes CONCURRENTLY (a small pool of browser tabs), so
a full sweep of the whole date window takes ~1 minute instead of ~8. That keeps
alerts close to real time across all dates.

SHARED USE
  One person runs this. Everyone else installs the free "ntfy" app and subscribes
  to a topic. It is NOTIFY-ONLY: it tells a human "go grab seat H14"; it never
  logs in, carts, holds, or buys anything.

TOPICS (subscribe in the ntfy app)
  odyssey_nyc_all           -> every day's openings
  odyssey_nyc_<mon><day>    -> just that day, e.g. odyssey_nyc_jul28
  The exact list prints on startup.

SETUP (one time)
  pip install playwright requests
  playwright install chromium
RUN
  python amc_seat_watcher.py        (keep awake: caffeinate -dimsu python ...)
  Ctrl+C to stop.
"""

import re
import os
import json
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
        return ""NTFY_TOKEN = _load_ntfy_token()

# ============================ CONFIG ============================

# Discovery listing page. Uses the movie-theatres URL format (same as CityWalk),
# which surfaces newly-released far-out dates the older discovery path missed.
LISTING_URL = ("https://www.amctheatres.com/movie-theatres/new-york-city/"
               "amc-lincoln-square-13/showtimes?date={date}")
THEATRE_LABEL = "AMC Lincoln Square 13"
THEATRE_TZ = "America/New_York"

# Date window (inclusive). START_DATE = None means today.
START_DATE = None
END_DATE = "2026-09-20"

# Format tags: discovery casts a wide net; the strict VERIFY_TAG is the real gate
# checked on each seat page, so ONLY true IMAX 70mm shows ever alert.
DISCOVERY_TAG = "70mm"
VERIFY_TAG = "imax70mm"

# How many showtimes to check at once. Higher = faster sweeps, more load on AMC.
CONCURRENCY = 4

MANUAL_SHOWTIMES = set()            # optionally pin extra showtime IDs by hand

NTFY_TOPIC_PREFIX = "odyssey_nyc"

FRONT_ROWS_TO_SKIP = {"A", "B", "C", "D"}          # e.g. {"A","B","C"}; empty = alert on any row
WANTED_TYPES = {"CanReserve"}       # normal seats; skips wheelchair/companion

POLL_SECONDS = 45                   # pause between sweeps (sweep itself is ~1 min)
REDISCOVER_SECONDS = 300            # re-scan which shows exist every 5 min
ALERT_COOLDOWN_SECONDS = 120      # changes-only-ish: NEW openings fire instantly;
                                    # a still-open seat re-pings at time set accordingly

# Persistence filter for "phantom" seats (show available but aren't buyable, e.g.
# stuck K6/K8/K9). A real cancellation gets grabbed and disappears fast; a phantom
# sits open forever. So: if a seat stays continuously open past PHANTOM_AFTER,
# treat it as a phantom and stop alerting on it. If it's STILL stuck past
# PHANTOM_RECHECK, give it one more chance (in case it became real) then re-suppress
# -- so a stuck seat pings at most about once an hour instead of constantly.
# NOTE: "continuously" resets if the seat disappears and comes back, so a genuine
# reopen still alerts immediately.
PHANTOM_AFTER_SECONDS = 600         # 10 min open non-stop -> treat as phantom, mute it
PHANTOM_RECHECK_SECONDS = 1800      # after 1 hr stuck, re-arm for one more alert

# Only alert when a show has at most this many open seats (the cancellation
# signal). Keeps alert volume under the free ntfy daily quota. 0 = no cap
# (only do that with a registered/paid ntfy account).
MAX_OPEN_TO_ALERT = 0

# Queue handling: AMC serves a tiny waiting-room page during drops. Detect it
# (page far smaller than a real one + no 70mm content) and wait/retry until the
# real site returns instead of running blind.
QUEUE_MIN_PAGE_SIZE = 120000    # real listing ~600K; queue stub ~35K
QUEUE_RETRY_SECONDS = 30        # re-probe this often while queued
QUEUE_MAX_WAIT_SECONDS = 3600   # give up waiting after this long, proceed anyway

SEAT_NAME_LIMIT = 500               # list every seat name (a full house is ~480)

HEADFUL = False

# State file: on your Mac the watcher runs forever so this memory lives in RAM.
# On GitHub Actions each run is a fresh process, so phantom-tracking and dedup
# state gets saved here at the end of every run and reloaded at the start of the
# next, so a stuck seat still gets muted and a seen seat still doesn't re-spam.
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state_nyc.json")

# ================================================================

ALL_TOPIC = f"{NTFY_TOPIC_PREFIX}_all"

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


def day_topic(date_str):
    d = dt.date.fromisoformat(date_str)
    return f"{NTFY_TOPIC_PREFIX}_{d.strftime('%b').lower()}{d.day}"


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


async def wait_out_queue(context, sem):
    """Probe AMC until the real site is served (not the queue/waiting-room page).
    Returns once the page is full-size with 70mm content, or after the max wait."""
    probe_date = date_range()[0]
    url = LISTING_URL.format(date=probe_date)
    waited = 0
    while True:
        try:
            html = await fetch_html(context, url, sem, wait_ms=1500)
        except Exception:
            html = ""
        if len(html) > QUEUE_MIN_PAGE_SIZE and "70mm" in html.lower():
            print(f"[{time.strftime('%H:%M:%S')}] site is live ({len(html)} chars). starting sweeps.")
            return
        if waited >= QUEUE_MAX_WAIT_SECONDS:
            print(f"[{time.strftime('%H:%M:%S')}] still queued after {waited}s -> proceeding anyway.")
            return
        print(f"[{time.strftime('%H:%M:%S')}] AMC queue active (got {len(html)} chars). "
              f"waiting {QUEUE_RETRY_SECONDS}s...")
        await asyncio.sleep(QUEUE_RETRY_SECONDS)
        waited += QUEUE_RETRY_SECONDS


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
    is_imax70 = VERIFY_TAG.lower() in html.lower()
    label = showtime_label(html, sid, date_str)
    open_seats, other = parse_seats(html)
    return (sid, date_str, is_imax70, open_seats, other, url, label)


# ------------------------------ main ------------------------------

def load_state():
    """Load alerted_at / open_since from disk. Missing or corrupt -> start clean."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("alerted_at", {}), data.get("open_since", {})
    except Exception:
        return {}, {}


def save_state(alerted_at, open_since):
    """Save state so the next run (fresh process on Actions) remembers it."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"alerted_at": alerted_at, "open_since": open_since}, f)
    except Exception as e:
        print(f"[warn] could not save state file: {e}")


async def run():
    window = date_range()
    print(f"Watching {THEATRE_LABEL} for IMAX 70mm shows ({CONCURRENCY} tabs)")
    print("Publishing: " + ("AUTHENTICATED (your ntfy account, higher quota)"
                            if NTFY_TOKEN else
                            "ANONYMOUS (low free quota - add a token to raise it)"))
    print(f"Date window: {window[0]} through {window[-1]}\n")
    print("Topics people can subscribe to in the ntfy app:")
    print(f"  ALL days    -> {ALL_TOPIC}")
    for d in window:
        print(f"  {pretty_day(d):<14}-> {day_topic(d)}")
    print()

    # Per date: {seat_name: last_time_we_alerted_it}. A seat only re-alerts if
    # it's brand new or its last alert was more than ALERT_COOLDOWN_SECONDS ago.
    # This kills flicker-spam (seats blinking in/out during checkout holds) that
    # otherwise fires an alert every sweep and drains the ntfy quota.
    alerted_at, open_since = load_state()   # carried over from the previous run
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

        # Single probe for the queue (don't burn the job's time budget waiting
        # on it -- if it's up, exit cleanly and let the next scheduled run try).
        probe_html = ""
        try:
            probe_html = await fetch_html(
                context, LISTING_URL.format(date=date_range()[0]), sem, wait_ms=1500)
        except Exception as e:
            print(f"[warn] initial probe failed: {e}")
        if len(probe_html) <= QUEUE_MIN_PAGE_SIZE or "70mm" not in probe_html.lower():
            print(f"[{time.strftime('%H:%M:%S')}] AMC queue/blocked "
                  f"(got {len(probe_html)} chars) -- skipping this run, "
                  f"will retry next schedule.")
            await browser.close()
            return

        shows = await discover(context, sem)
        print(f"[{time.strftime('%H:%M:%S')}] discovered {len(shows)} "
              f"candidate showtime(s)")

        # Check every discovered show once, alerting the instant each resolves.
        sent_this_sweep = 0
        tasks = [check_show(context, sid, ds, sem) for sid, ds in shows.items()]
        for fut in asyncio.as_completed(tasks):
            sid, date_str, is_imax70, open_list, other, url, label = await fut
            stamp = time.strftime("%H:%M:%S")
            if is_imax70 is None:
                continue  # load failed; already warned
            if not is_imax70:
                print(f"[{stamp}] show {sid}: skipped (not IMAX 70mm)")
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

            now = time.time()

            # --- Persistence (phantom) filter ---
            # Track how long each seat has been continuously open. Seats that
            # have sat open too long are treated as phantoms and muted; a seat
            # that vanished and came back has its streak reset (counts as new).
            streaks = open_since.setdefault(sid, {})
            current = set(open_list)
            for s in list(streaks):          # seat closed -> end its streak
                if s not in current:
                    del streaks[s]
            eligible = []
            phantom = []
            for s in open_list:
                if s not in streaks:
                    streaks[s] = now          # first sighting of this streak
                dur = now - streaks[s]
                if dur >= PHANTOM_RECHECK_SECONDS:
                    streaks[s] = now          # re-arm ~hourly: one more chance
                    eligible.append(s)
                elif dur >= PHANTOM_AFTER_SECONDS:
                    phantom.append(s)         # stuck open too long -> mute
                else:
                    eligible.append(s)

            line = f"OPEN -> {', '.join(eligible) if eligible else '(none eligible)'}"
            if phantom:
                line += f"  [phantom-muted: {', '.join(phantom)}]"
            print(f"[{stamp}] {label}: {line}")

            if not eligible:
                continue  # everything open here is a muted phantom

            # Per-seat de-dup: only alert on eligible seats that are new (or
            # went stale past the cooldown), so a flickering seat can't spam.
            seen = alerted_at.setdefault(sid, {})
            fresh = [s for s in eligible
                     if s not in seen or now - seen[s] > ALERT_COOLDOWN_SECONDS]
            if not fresh:
                continue  # already told people about these seats

            # Fire this show's alert immediately (day topic + all topic).
            if len(eligible) > 15:
                body = f"Lots of seats available ({len(eligible)})\n{url}"
            else:
                body = f"{fmt_seats(eligible)}\n{url}"
            sent = await asyncio.to_thread(
                notify, [day_topic(date_str), ALL_TOPIC],
                label, body, url)
            if sent:
                sent_this_sweep += sent
                print(f"  -> alert sent ({sent}/2): "
                      f"{day_topic(date_str)} + {ALL_TOPIC}")
            else:
                print("  -> alert NOT delivered (see warnings above)")
            for s in eligible:
                seen[s] = now

        if sent_this_sweep:
            print(f"[{time.strftime('%H:%M:%S')}] this run sent "
                  f"{sent_this_sweep} ntfy message(s)")

        await browser.close()

    # Trim state so the file doesn't grow forever: drop shows we haven't seen
    # open in a long while (their entries are dead weight once sold back out).
    save_state(alerted_at, open_since)
    print(f"[{time.strftime('%H:%M:%S')}] run complete. state saved.")


if __name__ == "__main__":
    asyncio.run(run())
