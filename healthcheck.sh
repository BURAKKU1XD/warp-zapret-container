#!/usr/bin/bash
set -euo pipefail

# A connected daemon is necessary, but not sufficient: test both public paths.
if ! /bin/busybox timeout 2 warp-cli --accept-tos status 2>/dev/null |
  grep -Fq 'Status update: Connected'; then
  echo "unhealthy: WARP is not Connected"
  exit 1
fi

probe() {
  local name="$1" proxy="$2" trace line
  # Ignore curlrc and ambient proxy bypass settings. Never print trace/IP data.
  if ! trace="$(curl --disable --silent --fail \
    --proxy "$proxy" --noproxy "" \
    --connect-timeout 3 --max-time 8 --max-filesize 4096 \
    --proto '=https' \
    --write-out '\n%{http_code}' \
    https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null)"; then
    echo "unhealthy: $name proxy HTTPS request failed"
    return 1
  fi
  if [ "${trace##*$'\n'}" != "200" ]; then
    echo "unhealthy: $name proxy HTTP status is not 200"
    return 1
  fi
  trace="${trace%$'\n'*}"
  while IFS= read -r line; do
    if [ "${line%$'\r'}" = "warp=on" ]; then
      return 0
    fi
  done <<< "$trace"
  echo "unhealthy: $name proxy response does not confirm warp=on"
  return 1
}

# Parallel requests keep the whole check within Docker's 12-second budget.
probe HTTP "http://127.0.0.1:${PUBLIC_HTTP_PROXY_PORT:-${PUBLIC_PROXY_PORT:-8888}}" &
http_pid=$!
probe SOCKS "socks5h://127.0.0.1:${PUBLIC_SOCKS_PROXY_PORT:-1080}" &
socks_pid=$!
failed=0
wait "$http_pid" || failed=1
wait "$socks_pid" || failed=1
if [ "$failed" = 1 ]; then
  exit 1
fi
echo "healthy: HTTP and SOCKS HTTPS verified, warp=on"
