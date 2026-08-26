#!/bin/zsh
# Supervisor for the NYC (Lincoln Square) watcher.
# Runs the watcher for RUN_SECONDS, then kills it AND its whole browser tree,
# waits COOLDOWN_SECONDS, and starts it fresh. Also catches crashes/OS-kills:
# if the watcher dies early, it still waits the cooldown and restarts.
#
# Launch in its own Terminal tab:   ./run_nyc.sh
# Stagger vs CityWalk by starting that one a couple hours later.
# Stop: Ctrl+C (the supervisor cleans up the watcher, then exits).

SCRIPT="amc_seat_watcher.py"
RUN_SECONDS=14400      # 4 hours
COOLDOWN_SECONDS=600   # 10 minutes

# Recursively kill a process and ALL its descendants (python -> node -> chromium
# -> chromium helpers). Walks the tree via `pgrep -P`, kills bottom-up. Only
# touches descendants of our own launched PID, so it never hits your other
# watcher or your personal Chrome.
kill_tree() {
  local pid=$1 child
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    kill_tree "$child"
  done
  kill -TERM "$pid" 2>/dev/null
  # brief grace, then force if still alive
  ( sleep 2; kill -KILL "$pid" 2>/dev/null ) 2>/dev/null &
}

cleanup() {
  if [[ -n "$PID" ]]; then
    echo "[$(date '+%H:%M:%S')] stopping watcher tree (pid $PID)..."
    kill_tree "$PID"
    wait "$PID" 2>/dev/null
  fi
}

# If YOU Ctrl+C the supervisor, take the watcher down with it, then exit.
trap 'echo; echo "supervisor stopping."; cleanup; exit 0' INT TERM

while true; do
  echo "[$(date '+%H:%M:%S')] starting $SCRIPT (runs up to ${RUN_SECONDS}s)..."
  caffeinate -i python3 "$SCRIPT" &
  PID=$!

  # Run for RUN_SECONDS, but break early if it crashes/gets killed on its own.
  SECONDS=0
  while (( SECONDS < RUN_SECONDS )); do
    if ! kill -0 "$PID" 2>/dev/null; then
      echo "[$(date '+%H:%M:%S')] watcher exited early (crash or OS kill)."
      break
    fi
    sleep 5
  done

  cleanup
  echo "[$(date '+%H:%M:%S')] cooldown ${COOLDOWN_SECONDS}s (no seat checks now)..."
  sleep "$COOLDOWN_SECONDS"
done
