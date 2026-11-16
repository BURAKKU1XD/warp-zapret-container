#!/usr/bin/bash
# Shared runtime functions; sourcing this file never starts services.

configure_dns() {
  local resolv_conf="${1:-/etc/resolv.conf}"
  local backup="${2:-/run/warp-zapret.resolv.conf.original}"
  local servers="${DNS_SERVERS:-1.1.1.1 8.8.8.8}" server octet
  if [ "$servers" = inherit ]; then
    echo "[+] DNS inherited from container runtime"
    return 0
  fi
  local -a nameservers octets
  servers="${servers//$'\n'/ }"
  IFS=$' \t\r' read -r -a nameservers <<< "$servers"
  if [ "${#nameservers[@]}" -lt 1 ] || [ "${#nameservers[@]}" -gt 3 ]; then
    echo "[!] DNS_SERVERS requires 1 to 3 space-separated IPv4 addresses" >&2
    return 2
  fi
  for server in "${nameservers[@]}"; do
    if ! [[ "$server" =~ ^(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2})){3}$ ]]; then
      echo "[!] DNS_SERVERS requires IPv4 addresses" >&2
      return 2
    fi
    IFS=. read -r -a octets <<< "$server"
    for octet in "${octets[@]}"; do
      if [ "$octet" -gt 255 ]; then
        echo "[!] DNS_SERVERS contains an invalid IPv4 address" >&2
        return 2
      fi
    done
  done
  # Keep the original outside persistent WARP state; never replace a bind mount.
  if [ ! -e "$backup" ]; then
    (umask 077; cat "$resolv_conf" > "$backup") || return 1
  fi
  if ! printf 'nameserver %s\n' "${nameservers[@]}" > "$resolv_conf"; then
    echo "[!] Cannot configure DNS in $resolv_conf" >&2
    return 1
  fi
  echo "[+] DNS configured: ${nameservers[*]}"
}

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
