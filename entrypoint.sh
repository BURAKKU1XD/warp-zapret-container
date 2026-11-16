#!/usr/bin/env bash
set -Eeuo pipefail

STRATEGY="${ZAPRET_STRATEGY:-general}"
STRATEGY_EXPLICIT=0
DRY_RUN=0

list_strategies() {
  local file
  for file in /config/strategies/*.conf; do
    [ -f "$file" ] || continue
    file="${file##*/}"
    printf '%s\n' "${file%.conf}"
  done | sort
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --strategy)
      if [ "$#" -lt 2 ] || [ -z "$2" ] || [[ "$2" == --* ]]; then
        echo "[!] --strategy requires a name (see --list-strategies)" >&2
        exit 2
      fi
      STRATEGY="$2"
      STRATEGY_EXPLICIT=1
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --list-strategies)
      list_strategies
      exit 0
      ;;
    --help|-h)
      echo "Usage: /entrypoint.sh [--strategy NAME] [--dry-run]"
      echo "       /entrypoint.sh --list-strategies"
      echo "Selection: --strategy > CONFIG > ZAPRET_STRATEGY > general"
      exit 0
      ;;
    *)
      echo "[!] Unknown argument: $1 (see --help)" >&2
      exit 2
      ;;
  esac
done

if [ "$STRATEGY_EXPLICIT" = 0 ] && [ -n "${CONFIG:-}" ]; then
  STRATEGY="custom"
else
  if ! [[ "$STRATEGY" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] ||
    [ ! -f "/config/strategies/$STRATEGY.conf" ]; then
    echo "[!] Unknown strategy: $STRATEGY (see --list-strategies)" >&2
    exit 2
  fi
  CONFIG="/config/strategies/$STRATEGY.conf"
fi

if [ ! -r "$CONFIG" ]; then
  echo "[!] Cannot read config: $CONFIG" >&2
  exit 2
fi

WARP_PROXY_PORT="${WARP_PROXY_PORT:-40000}"

PUBLIC_HTTP_PROXY_PORT="${PUBLIC_HTTP_PROXY_PORT:-${PUBLIC_PROXY_PORT:-8888}}"
PUBLIC_SOCKS_PROXY_PORT="${PUBLIC_SOCKS_PROXY_PORT:-1080}"

source "$CONFIG"

if [ "$DRY_RUN" = 1 ]; then
  exec /usr/local/bin/nfqws --dry-run --qnum="$NFQUEUE_NUM" "${NFQWS_ARGS[@]}"
fi

source /runtime.sh

cleanup_rules() {
  while iptables -t mangle -D OUTPUT \
    -p udp -m multiport --dports "$NFQWS_PORTS_UDP" ! -d 127.0.0.0/8 \
    -m connbytes --connbytes "$NFQWS_UDP_CONNBYTES" \
    --connbytes-mode packets \
    --connbytes-dir original \
    -j NFQUEUE --queue-num "$NFQUEUE_NUM" --queue-bypass 2>/dev/null; do true; done

  while iptables -t mangle -D OUTPUT \
    -p tcp -m multiport --dports "$NFQWS_PORTS_TCP" ! -d 127.0.0.0/8 \
    -m connbytes --connbytes "$NFQWS_TCP_CONNBYTES" \
    --connbytes-mode packets \
    --connbytes-dir original \
    -j NFQUEUE --queue-num "$NFQUEUE_NUM" --queue-bypass 2>/dev/null; do true; done

  while iptables -t mangle -D OUTPUT \
    -m mark --mark 0x40000000/0x40000000 \
    -m comment --comment warp-zapret1-generated \
    -j ACCEPT 2>/dev/null; do true; done
}

setup_rules() {
  cleanup_rules

  # nfqws-generated packets must bypass NFQUEUE (default desync fwmark).
  iptables -t mangle -I OUTPUT 1 \
    -m mark --mark 0x40000000/0x40000000 \
    -m comment --comment warp-zapret1-generated \
    -j ACCEPT

  echo "[+] queue UDP ports $NFQWS_PORTS_UDP, packets $NFQWS_UDP_CONNBYTES"
  iptables -t mangle -A OUTPUT \
    -p udp -m multiport --dports "$NFQWS_PORTS_UDP" ! -d 127.0.0.0/8 \
    -m connbytes --connbytes "$NFQWS_UDP_CONNBYTES" \
    --connbytes-mode packets \
    --connbytes-dir original \
    -j NFQUEUE --queue-num "$NFQUEUE_NUM" --queue-bypass

  echo "[+] queue TCP ports $NFQWS_PORTS_TCP, packets $NFQWS_TCP_CONNBYTES"
  iptables -t mangle -A OUTPUT \
    -p tcp -m multiport --dports "$NFQWS_PORTS_TCP" ! -d 127.0.0.0/8 \
    -m connbytes --connbytes "$NFQWS_TCP_CONNBYTES" \
    --connbytes-mode packets \
    --connbytes-dir original \
    -j NFQUEUE --queue-num "$NFQUEUE_NUM" --queue-bypass
}

kill_pid() {
  local pid="${1:-}"
  if [ -n "$pid" ]; then
    kill "$pid" 2>/dev/null || true
  fi
}

cleanup() {
  set +e

  echo "[+] cleanup"

  kill_pid "${WATCHDOG_PID:-}"
  kill_pid "${SOCAT_HTTP_PID:-}"
  kill_pid "${SOCAT_SOCKS_PID:-}"
  kill_pid "${SOCAT_PID:-}"
  kill_pid "${WARP_PID:-}"
  kill_pid "${NFQWS_PID:-}"

  cleanup_rules
}
# Runtime starts here.
trap cleanup EXIT
trap 'exit 0' TERM
trap 'exit 130' INT
trap 'exit 1' USR1

rm -f /run/warp-zapret.ready
watchdog "$$" /run/warp-zapret.ready &
WATCHDOG_PID=$!

echo "[+] strategy: $STRATEGY ($CONFIG)"
echo "[+] checking selected config and assets"
/usr/local/bin/nfqws --dry-run --qnum="$NFQUEUE_NUM" "${NFQWS_ARGS[@]}"

echo "[+] starting dbus"
mkdir -p /run/dbus
rm -f /run/dbus/pid
dbus-daemon --system --fork || true

echo "[+] starting nfqws"

/usr/local/bin/nfqws \
  --qnum="$NFQUEUE_NUM" \
  "${NFQWS_ARGS[@]}" \
  &

NFQWS_PID="$!"

sleep 1

if ! kill -0 "$NFQWS_PID" 2>/dev/null; then
  echo "[!] nfqws failed"
  exit 1
fi

setup_rules

iptables -t mangle -vnL OUTPUT

echo "[+] starting warp-svc"
rm -f /var/lib/cloudflare-warp/mdm.xml 2>/dev/null || true

warp-svc > >(filter_warp_log) 2>&1 &
WARP_PID="$!"

echo "[+] waiting for warp-svc"
DAEMON_READY=0
for i in $(seq 1 30); do
  if warp_cli status >/dev/null 2>&1; then
    DAEMON_READY=1
    echo "[+] warp-svc is ready"
    break
  fi
  sleep 1
done

if [ "$DAEMON_READY" != 1 ]; then
  echo "[!] warp-svc did not become ready" >&2
  exit 1
fi

echo "[+] checking WARP registration"
if ! warp_cli registration show >/dev/null 2>&1; then
  echo "[+] WARP is not registered, creating registration through zapret1"
  if ! warp_cli registration new >/dev/null 2>&1; then
    echo "[!] WARP registration failed or timed out" >&2
    exit 1
  fi
fi

echo "[+] applying Windows-equivalent WARP settings"
warp_cli disconnect || true
warp_cli tunnel endpoint reset || true
warp_cli tunnel protocol set MASQUE
warp_cli tunnel masque-options set h3-with-h2-fallback
warp_cli mode proxy
warp_cli proxy port "$WARP_PROXY_PORT"

echo "[+] WARP settings"
warp_cli settings | grep -Ei -A12 'Mode|WARP tunnel protocol|MASQUE|Post-quantum|PMTUD' || true

echo "[+] connecting WARP"
warp_cli connect

echo "[+] waiting for Connected + local proxy"
CONNECTED=0
for i in $(seq 1 90); do
  if warp_cli status 2>/dev/null | grep -q 'Status update: Connected'; then
    if ss -lntup | grep -q ":$WARP_PROXY_PORT"; then
      CONNECTED=1
      break
    fi
  fi
  sleep 1
done

echo "[+] WARP status"
warp_cli status || true
warp_cli tunnel stats || true
ss -tunap | grep warp || true
ss -lntup | grep -E "$WARP_PROXY_PORT|$PUBLIC_HTTP_PROXY_PORT|$PUBLIC_SOCKS_PROXY_PORT" || true

if [ "$CONNECTED" != "1" ]; then
  echo "[!] WARP did not become Connected or local proxy did not listen"
  echo "[!] iptables:"
  iptables -t mangle -vnL OUTPUT || true
  exit 1
fi

echo "[+] exposing HTTP proxy 0.0.0.0:$PUBLIC_HTTP_PROXY_PORT -> 127.0.0.1:$WARP_PROXY_PORT"
socat TCP-LISTEN:"$PUBLIC_HTTP_PROXY_PORT",fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:"$WARP_PROXY_PORT" &
SOCAT_HTTP_PID="$!"

echo "[+] exposing SOCKS proxy 0.0.0.0:$PUBLIC_SOCKS_PROXY_PORT -> 127.0.0.1:$WARP_PROXY_PORT"
socat TCP-LISTEN:"$PUBLIC_SOCKS_PROXY_PORT",fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:"$WARP_PROXY_PORT" &
SOCAT_SOCKS_PID="$!"

: > /run/warp-zapret.ready
echo "[+] ready"
echo "[+] test HTTP from host:  curl -x http://127.0.0.1:$PUBLIC_HTTP_PROXY_PORT https://www.cloudflare.com/cdn-cgi/trace"
echo "[+] test SOCKS from host: curl --socks5-hostname 127.0.0.1:$PUBLIC_SOCKS_PROXY_PORT https://www.cloudflare.com/cdn-cgi/trace"

while true; do
  if ! kill -0 "$NFQWS_PID" 2>/dev/null; then
    echo "[!] nfqws exited"
    exit 1
  fi

  if ! kill -0 "$WARP_PID" 2>/dev/null; then
    echo "[!] warp-svc exited"
    exit 1
  fi

  if ! kill -0 "$SOCAT_HTTP_PID" 2>/dev/null; then
    echo "[!] HTTP socat exited"
    exit 1
  fi

  if ! kill -0 "$SOCAT_SOCKS_PID" 2>/dev/null; then
    echo "[!] SOCKS socat exited"
    exit 1
  fi

  if ! kill -0 "$WATCHDOG_PID" 2>/dev/null; then
    echo "[!] watchdog exited"
    exit 1
  fi

  sleep 5
done
