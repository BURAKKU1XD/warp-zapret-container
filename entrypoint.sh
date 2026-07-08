#!/usr/bin/env bash
set -Eeuo pipefail

CONFIG="${CONFIG:-/config/zapret1-warp.conf}"

NFQWS_LOG="/var/log/zapret1-warp-nfqws.log"
WARP_LOG="/var/log/warp-svc.log"

WARP_PROXY_PORT="${WARP_PROXY_PORT:-40000}"

PUBLIC_HTTP_PROXY_PORT="${PUBLIC_HTTP_PROXY_PORT:-${PUBLIC_PROXY_PORT:-8888}}"
PUBLIC_SOCKS_PROXY_PORT="${PUBLIC_SOCKS_PROXY_PORT:-1080}"

source "$CONFIG"

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

  kill_pid "${SOCAT_HTTP_PID:-}"
  kill_pid "${SOCAT_SOCKS_PID:-}"
  kill_pid "${SOCAT_PID:-}"
  kill_pid "${WARP_PID:-}"
  kill_pid "${NFQWS_PID:-}"

  cleanup_rules
}
trap cleanup INT TERM EXIT

echo "[+] starting dbus"
mkdir -p /run/dbus
rm -f /run/dbus/pid
dbus-daemon --system --fork || true

echo "[+] cleanup iptables"
cleanup_rules

echo "[+] checking assets"
test -s /config/lists/list-general.txt
test -s /config/lists/ipset-all.txt
test -s /opt/zapret/files/fake/quic_initial_www_google_com.bin
test -s /opt/zapret/files/fake/tls_clienthello_www_google_com.bin
test -s /opt/zapret/files/fake/stun.bin

echo "[+] starting nfqws"
: > "$NFQWS_LOG"

/usr/local/bin/nfqws \
  --qnum="$NFQUEUE_NUM" \
  "${NFQWS_ARGS[@]}" \
  > "$NFQWS_LOG" 2>&1 &

NFQWS_PID="$!"

sleep 1

if ! kill -0 "$NFQWS_PID" 2>/dev/null; then
  echo "[!] nfqws failed"
  tail -n 200 "$NFQWS_LOG" || true
  exit 1
fi

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

iptables -t mangle -vnL OUTPUT

echo "[+] starting warp-svc"
rm -f /var/lib/cloudflare-warp/mdm.xml 2>/dev/null || true

: > "$WARP_LOG"
warp-svc > "$WARP_LOG" 2>&1 &
WARP_PID="$!"

echo "[+] waiting for warp-svc"
for i in $(seq 1 30); do
  if warp-cli --accept-tos status >/dev/null 2>&1; then
    echo "[+] warp-svc is ready"
    break
  fi
  sleep 1
done

echo "[+] checking WARP registration"
if ! warp-cli --accept-tos registration show; then
  echo "[+] WARP is not registered, creating registration through zapret1"
  warp-cli --accept-tos registration new
  warp-cli --accept-tos registration show
fi

echo "[+] applying Windows-equivalent WARP settings"
warp-cli --accept-tos disconnect || true
warp-cli --accept-tos tunnel endpoint reset || true
warp-cli --accept-tos tunnel protocol set MASQUE
warp-cli --accept-tos tunnel masque-options set h3-with-h2-fallback
warp-cli --accept-tos mode proxy
warp-cli --accept-tos proxy port "$WARP_PROXY_PORT"

echo "[+] WARP settings"
warp-cli --accept-tos settings | grep -Ei -A12 'Mode|WARP tunnel protocol|MASQUE|Post-quantum|PMTUD' || true

echo "[+] connecting WARP"
warp-cli --accept-tos connect

echo "[+] waiting for Connected + local proxy"
CONNECTED=0
for i in $(seq 1 90); do
  if warp-cli --accept-tos status 2>/dev/null | grep -q 'Status update: Connected'; then
    if ss -lntup | grep -q ":$WARP_PROXY_PORT"; then
      CONNECTED=1
      break
    fi
  fi
  sleep 1
done

echo "[+] WARP status"
warp-cli --accept-tos status || true
warp-cli --accept-tos tunnel stats || true
ss -tunap | grep warp || true
ss -lntup | grep -E "$WARP_PROXY_PORT|$PUBLIC_HTTP_PROXY_PORT|$PUBLIC_SOCKS_PROXY_PORT" || true

if [ "$CONNECTED" != "1" ]; then
  echo "[!] WARP did not become Connected or local proxy did not listen"
  echo "[!] iptables:"
  iptables -t mangle -vnL OUTPUT || true
  echo "[!] nfqws log:"
  tail -n 240 "$NFQWS_LOG" || true
  echo "[!] warp log:"
  tail -n 260 "$WARP_LOG" || true
  exit 1
fi

echo "[+] exposing HTTP proxy 0.0.0.0:$PUBLIC_HTTP_PROXY_PORT -> 127.0.0.1:$WARP_PROXY_PORT"
socat TCP-LISTEN:"$PUBLIC_HTTP_PROXY_PORT",fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:"$WARP_PROXY_PORT" &
SOCAT_HTTP_PID="$!"

echo "[+] exposing SOCKS proxy 0.0.0.0:$PUBLIC_SOCKS_PROXY_PORT -> 127.0.0.1:$WARP_PROXY_PORT"
socat TCP-LISTEN:"$PUBLIC_SOCKS_PROXY_PORT",fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:"$WARP_PROXY_PORT" &
SOCAT_SOCKS_PID="$!"

echo "[+] ready"
echo "[+] test HTTP from host:  curl -x http://127.0.0.1:$PUBLIC_HTTP_PROXY_PORT https://www.cloudflare.com/cdn-cgi/trace"
echo "[+] test SOCKS from host: curl --socks5-hostname 127.0.0.1:$PUBLIC_SOCKS_PROXY_PORT https://www.cloudflare.com/cdn-cgi/trace"

while true; do
  if ! kill -0 "$NFQWS_PID" 2>/dev/null; then
    echo "[!] nfqws exited"
    tail -n 200 "$NFQWS_LOG" || true
    exit 1
  fi

  if ! kill -0 "$WARP_PID" 2>/dev/null; then
    echo "[!] warp-svc exited"
    tail -n 260 "$WARP_LOG" || true
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

  sleep 5
done
