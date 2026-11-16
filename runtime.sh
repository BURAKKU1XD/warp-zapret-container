#!/usr/bin/bash
# Shared runtime functions; sourcing this file never starts services.

filter_warp_log() {
  local line
  while IFS= read -r line || [ -n "$line" ]; do
    # Registration commands are silenced separately. Drop sensitive daemon fields.
    case "${line,,}" in
      *registration*|*token*|*key*|*license*|*account*|*secret*|*device*|*client_id*|*mdm*) continue ;;
    esac
    printf '%s\n' "$line"
  done
}

warp_cli() {
  /bin/busybox timeout -k 2 10 warp-cli --accept-tos "$@" 2>&1 | filter_warp_log
}

watchdog() {
  local parent="$1" ready="$2" grace="${3:-120}" interval="${4:-30}"
  local failures=0 child=""
  trap 'if [ -n "$child" ]; then kill "$child" 2>/dev/null || true; fi; exit 0' TERM INT

  sleep "$grace" &
  child=$!
  wait "$child" || return
  child=""
  if [ ! -f "$ready" ]; then
    echo "[!] startup exceeded ${grace}s; restarting container"
    kill -USR1 "$parent"
    return
  fi

  while true; do
    /bin/busybox timeout -k 1 11 /healthcheck.sh >/dev/null 2>&1 &
    child=$!
    if wait "$child"; then
      failures=0
    else
      failures=$((failures + 1))
      echo "[!] watchdog: healthcheck failed ($failures/5)"
    fi
    child=""
    if [ "$failures" -ge 5 ]; then
      echo "[!] five consecutive healthcheck failures; restarting container"
      kill -USR1 "$parent"
      return
    fi
    sleep "$interval" &
    child=$!
    wait "$child" || return
    child=""
  done
}
