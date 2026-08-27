# AMC IMAX 70mm Seat Tracker

![Python](https://img.shields.io/badge/python-3.11-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-active-brightgreen)

> Get pinged the instant an AMC IMAX 70mm seat opens up — even on shows that are otherwise sold out.

A notification-only tool that watches AMC showtimes for **IMAX 70mm** screenings, checks their seat maps, and pushes a phone alert the moment a seat frees up. It does **not** log in, add to cart, hold, or purchase anything. It just tells a human "seat H14 is open, go grab it," via [ntfy](https://ntfy.sh).

Runs automatically on GitHub Actions — no personal machine required.

---



## Contents

- [How It Works](#how-it-works)
- [Current Locations](#current-locations)
- [Notifications](#notifications)
- [Seat Filtering](#seat-filtering)
- [Phantom Seat Detection](#phantom-seat-detection)
- [State Persistence](#state-persistence)
- [Running Locally](#running-locally)
- [GitHub Actions](#github-actions)
- [Automated Scheduling](#automated-scheduling)
- [Configuration](#configuration)
- [NTFY Authentication](#ntfy-authentication)
- [Project Structure](#project-structure)
- [Adding Another Location](#adding-another-location)
- [Disclaimer](#disclaimer)
- [License](#license)

---



## How It Works

1. **Discovery** — loads AMC's showtime listing pages for a date range and finds candidate showtimes.
2. **Verification** — for each candidate, loads its seat page and checks for the `imax70mm` tag. Discovery casts a wide net (`70mm`); only shows that verify as true IMAX 70mm ever alert.
3. **Seat parsing** — AMC embeds the full seat map as JSON directly in the page HTML. The tracker reads it to find seats that are `available`, of a wanted type (normal reservable seats, not wheelchair/companion), and not in a skipped row.
4. **Phantom filtering** — some seats show as "available" but aren't actually buyable (stuck/held seats). A seat that stays continuously open past a threshold is treated as a phantom and muted; if it disappears and reopens later, it's treated as a fresh opening again.
5. **Notification** — a genuinely new, eligible opening triggers a push notification via ntfy, with the seat names (or a count, if it's a large drop) and a direct link to the seat page.



## Current Locations



### New York City — AMC Lincoln Square 13

- File: `NYC_seat_watcher_actions.py`
- Timezone: `America/New_York`
- State file: `state_nyc.json`

**Los Angeles — AMC Universal Cinema (CityWalk)**

- File: `CITYWALK_seat_watcher_actions.py`
- Timezone: `America/Los_Angeles` 
- State file: `state_citywalk.json`

Additional locations (e.g. Los Angeles) are planned but not yet converted to this architecture — see [Adding Another Location](#adding-another-location).

## Notifications

Alerts are sent through [ntfy](https://ntfy.sh), a free push-notification service. To receive alerts:

1. Install the ntfy app (iOS/Android) or use a browser.
2. Subscribe to a topic:
  - `odyssey_nyc_all` — every opening, every date
  - `odyssey_nyc_<mon><day>` (e.g. `odyssey_nyc_aug17`) — just one specific date
  - `odyssey_citywalk_all` — CityWalk (Los Angeles), every opening

Each alert includes the showtime, the open seat(s) (or a count for large openings), and a tap-through link straight to that seat page.

## Seat Filtering

Configurable in the script:

```python
FRONT_ROWS_TO_SKIP = {"A", "B", "C", "D"}   # rows to never alert on
WANTED_TYPES = {"CanReserve"}                # normal seats only
MAX_OPEN_TO_ALERT = 0                        # 0 = no cap on seat count
```



## Phantom Seat Detection

AMC occasionally reports a seat as available when it isn't actually purchasable (confirmed at the box office in some cases — seats that sit "open" indefinitely). The tracker distinguishes a real opening from a phantom by how long a seat stays continuously open:

```python
PHANTOM_AFTER_SECONDS = 600     # open 10+ min straight -> treat as phantom, mute it
PHANTOM_RECHECK_SECONDS = 1800  # still stuck after 30 min -> one more chance, then re-mute
```

A real cancellation gets grabbed and disappears quickly, so it never reaches the phantom threshold. A seat that disappears and later reopens is treated as brand new, so a genuine reopening still alerts immediately.

## State Persistence

The tracker runs as a **single sweep per invocation** on GitHub Actions (each run is a fresh process, not a long-running loop). To keep phantom-tracking and notification de-duplication working across runs, state is saved to a JSON file after every run and reloaded at the start of the next:

```
state_nyc.json
```

This file is committed back to the repository automatically by the workflow after each run. It won't change on every single run — only when the tracked seat state actually changes — so an unchanged `state_nyc.json` between runs is expected, not a sign of failure.

## Running Locally

```bash
pip install -r requirements.txt
playwright install chromium
python NYC_seat_watcher_actions.py
```

The token can be supplied either as an environment variable or a local file (see [NTFY Authentication](#ntfy-authentication)).

### Requirements

- Python 3.11+
- Playwright (with Chromium)
- `requests`



## GitHub Actions

The tracker runs on GitHub Actions rather than a personal machine. Each run:

1. Checks out the repo
2. Sets up Python and installs dependencies (including Chromium via Playwright)
3. Runs the watcher script once (one full sweep, then exits)
4. Commits the updated `state_nyc.json` back to the repo, if it changed

Workflow file: `tracker.yml`. It can be triggered manually from the Actions tab (`workflow_dispatch`) at any time.

## Automated Scheduling

GitHub's built-in `schedule` trigger was tried first but proved unreliable in testing — scheduled runs didn't consistently fire. The tracker is instead triggered by an external scheduler, **[cron-job.org](https://cron-job.org)**, which makes a `POST` request to GitHub's `workflow_dispatch` API on a fixed interval:

```
POST https://api.github.com/repos/rdFuse/imax70mm-seat-alerts/actions/workflows/tracker.yml/dispatches
Headers:
  Accept: application/vnd.github+json
  Authorization: Bearer <your GitHub personal access token>
  X-GitHub-Api-Version: 2026-03-10
Body:
  { "ref": "main" }
```



Since a full workflow run (checkout, dependency install, Chromium setup, the sweep itself, and the state commit) takes roughly 6 minutes, the external schedule is set to run every **8 minutes** to leave a margin against overlapping runs. This is a practical spacing choice, not a hard guarantee — a concurrency guard is a reasonable future addition if overlapping runs become an issue:

```yaml
concurrency:
  group: amc-nyc-seat-tracker
  cancel-in-progress: false
```



## Configuration

Key settings live near the top of the watcher script:


| Setting                                             | Purpose                                                                 |
| --------------------------------------------------- | ----------------------------------------------------------------------- |
| `START_DATE` / `END_DATE`                           | Date window to watch (inclusive)                                        |
| `CONCURRENCY`                                       | How many showtimes to check in parallel per run                         |
| `MAX_OPEN_TO_ALERT`                                 | Skip alerting on shows with more open seats than this (`0` = no cap)    |
| `ALERT_COOLDOWN_SECONDS`                            | Minimum time before re-alerting on the same seat                        |
| `PHANTOM_AFTER_SECONDS` / `PHANTOM_RECHECK_SECONDS` | Phantom-seat muting thresholds                                          |
| `FRONT_ROWS_TO_SKIP`                                | Rows to exclude from alerts                                             |
| `QUEUE_MIN_PAGE_SIZE`                               | Used to detect AMC's virtual waiting-room page during high-demand drops |


If AMC serves its queue/waiting-room page instead of real content (common during a new-showtime release), the run detects this (an unexpectedly small page with no 70mm content) and exits cleanly rather than alerting on bad data — the next scheduled run tries again.

## NTFY Authentication

Publishing uses a bearer token so alerts count against an authenticated ntfy account (higher quota, and the ability to lock topics against unwanted posts). The token is never committed to the repository.

**On GitHub Actions:** supplied as a repository secret, `NTFY_TOKEN`, injected as an environment variable:

```yaml
env:
  NTFY_TOKEN: ${{ secrets.NTFY_TOKEN }}
```

**Locally:** either set the same `NTFY_TOKEN` environment variable, or place the token in a local `ntfy_token.txt` file next to the script (excluded from git via `.gitignore`).

## Project Structure

```
.
├── NYC_seat_watcher_actions.py   # NYC watcher (single-sweep, GitHub Actions-ready)
├── state_nyc.json                # persisted state between runs (auto-committed)
├── tracker.yml                   # GitHub Actions workflow
├── requirements.txt              # Python dependencies
└── README.md
```

## Adding Another Location

The architecture is designed so each location is fully independent:

```
<city>_seat_watcher_actions.py
state_<city>.json
tracker_<city>.yml
```

Each location gets its own workflow, its own state file, and its own scheduler entry, so they never interfere with each other and can run concurrently. To add one: copy the NYC watcher, update the theatre URL, timezone, and ntfy topic prefix, then add a matching workflow file and a corresponding external scheduler trigger.

## GitHub Actions + Multiple Locations

Each location's workflow is triggered independently — dispatching one does not affect or block the others, since they run as separate jobs with separate state files.

## Disclaimer

This tool is notification-only. It reads publicly available seat-availability data and does not automate any purchase, login, or cart action. It's intended to help people catch legitimate cancellations on sold-out shows, not to gain an unfair advantage through automated purchasing.

## License

MIT