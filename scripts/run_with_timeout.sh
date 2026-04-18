#!/usr/bin/env bash
# Usage: run_with_timeout.sh <seconds> <command...>
# Runs the command; if it exceeds the timeout, sends SIGTERM (grace 5s) then SIGKILL.
# Portable alternative to GNU `timeout` — works on bare macOS.
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <seconds> <command...>" >&2
  exit 2
fi

LIMIT="$1"; shift

"$@" &
CHILD=$!

(
  sleep "$LIMIT"
  if kill -0 "$CHILD" 2>/dev/null; then
    echo "[run_with_timeout] command exceeded ${LIMIT}s — sending SIGTERM to pid $CHILD" >&2
    kill -TERM "$CHILD" 2>/dev/null || true
    sleep 5
    if kill -0 "$CHILD" 2>/dev/null; then
      echo "[run_with_timeout] still alive — SIGKILL" >&2
      kill -KILL "$CHILD" 2>/dev/null || true
    fi
  fi
) &
WATCHDOG=$!
disown $WATCHDOG 2>/dev/null || true

wait "$CHILD" 2>/dev/null
EXIT=$?

kill "$WATCHDOG" 2>/dev/null || true
exit "$EXIT"
