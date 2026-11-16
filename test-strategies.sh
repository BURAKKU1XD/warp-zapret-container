#!/usr/bin/env bash
# Host-side runner. Requires Bash 4+, Docker and curl; never sources .env.
set -Eeuo pipefail
umask 077

IMAGE="ghcr.io/burakku1xd/warp-zapret-container:latest"
PULL=missing
SELECTED=""
ROUNDS=3
STARTUP_TIMEOUT=150
REQUEST_TIMEOUT=10
OUTPUT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/test-results"
TARGETS_FILE=""
TARGETS=(
  https://discord.com/
  https://www.youtube.com/
  https://www.google.com/generate_204
  https://www.gstatic.com/generate_204
)

usage() {
  cat <<'EOF'
Usage: bash test-strategies.sh [options]
  --image IMAGE             Default: ghcr.io/burakku1xd/warp-zapret-container:latest
  --pull missing|always|never
  --strategies general,alt2 Default: all strategies from the image
  --rounds N                Default: 3
  --startup-timeout SECONDS Default: 150
  --request-timeout SECONDS Default: 10
  --targets-file FILE       One public HTTPS URL per line; # comments allowed
  --output DIR             Parent directory for reports (default: ./test-results)
  --help
Exit codes: 0 = at least one full PASS, 1 = no full PASS,
            2 = runner/preflight error, 130 = interrupted.
Requires a local Linux/amd64 Docker daemon and /dev/net/tun.
Tests create a temporary WARP registration and accept Cloudflare's terms,
as the normal image entrypoint does. Only runner-owned resources are removed.
EOF
}

die() { echo "Error: $*" >&2; exit 2; }
while (($#)); do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --image|--pull|--strategies|--rounds|--startup-timeout|--request-timeout|--targets-file|--output)
      (($# >= 2)) && [[ -n "$2" && "$2" != --* ]] || die "$1 requires a value"
      case "$1" in
        --image) IMAGE=$2 ;; --pull) PULL=$2 ;; --strategies) SELECTED=$2 ;;
        --rounds) ROUNDS=$2 ;; --startup-timeout) STARTUP_TIMEOUT=$2 ;;
        --request-timeout) REQUEST_TIMEOUT=$2 ;; --targets-file) TARGETS_FILE=$2 ;;
        --output) OUTPUT=$2 ;;
      esac
      shift 2 ;;
    *) die "Unknown option: $1" ;;
  esac
done
[[ "$PULL" =~ ^(missing|always|never)$ ]] || die "Invalid --pull"
for value in "$ROUNDS" "$STARTUP_TIMEOUT" "$REQUEST_TIMEOUT"; do
  [[ "$value" =~ ^[1-9][0-9]{0,4}$ ]] || die "Timeouts and rounds must be positive integers <= 99999"
done
[[ "$IMAGE" != -* && "$IMAGE" != *$'\n'* && "$IMAGE" != *$'\t'* ]] || die "Invalid image name"
if [[ -n "$TARGETS_FILE" ]]; then
  [[ -r "$TARGETS_FILE" ]] || die "Cannot read targets file"
  TARGETS=()
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    TARGETS+=("$line")
  done < "$TARGETS_FILE"
fi
((${#TARGETS[@]})) || die "Targets file is empty"
for url in "${TARGETS[@]}"; do
  # No credentials, query strings, whitespace or control characters in reports.
  [[ "$url" =~ ^https://[a-zA-Z0-9.-]+(:[0-9]+)?(/[a-zA-Z0-9/._~-]*)?$ ]] ||
    die "Targets must be public HTTPS URLs without credentials or query strings"
done
for command in docker curl; do
  command -v "$command" >/dev/null || die "$command is required"
done
[[ "$(docker info --format '{{.OSType}}')" == linux ]] || die "A Linux Docker daemon is required"
if [[ "$PULL" == always ]]; then
  docker pull "$IMAGE" || die "Image pull failed"
elif ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  [[ "$PULL" != never ]] || die "Image is not available locally"
  docker pull "$IMAGE" || die "Image pull failed"
fi
[[ "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$IMAGE")" == linux/amd64 ]] ||
  die "The image must be linux/amd64"
IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE")"
CATALOG="$(docker run --rm --network none "$IMAGE_ID" --list-strategies)" ||
  die "Cannot list strategies"
STRATEGIES=()
while IFS= read -r name; do
  [[ "$name" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] || die "Invalid strategy catalog"
  STRATEGIES+=("$name")
done <<< "$CATALOG"
if [[ -n "$SELECTED" ]]; then
  [[ "$SELECTED" =~ ^[a-z0-9-]+(,[a-z0-9-]+)*$ ]] || die "Invalid --strategies"
  IFS=, read -r -a requested <<< "$SELECTED"
  STRATEGIES=()
  for name in "${requested[@]}"; do
    grep -Fxq -- "$name" <<< "$CATALOG" || die "Unknown strategy: $name"
    [[ " ${STRATEGIES[*]} " == *" $name "* ]] || STRATEGIES+=("$name")
  done
fi

RUN_ID="warp-zapret-test-$(date -u +%Y%m%dT%H%M%S)-$$-$RANDOM"
REPORT="$OUTPUT/$RUN_ID"
mkdir -p -- "$OUTPUT"
mkdir -- "$REPORT"
SUMMARY="$REPORT/summary.tsv"
DETAILS="$REPORT/checks.tsv"
printf 'strategy\tstatus\tstartup_seconds\tpassed\ttotal\n' > "$SUMMARY"
printf 'strategy\tround\tproxy\ttarget\tstatus\thttp_code\tseconds\tcurl_exit\n' > "$DETAILS"
printf 'image=%s\nimage_id=%s\nrounds=%s\nstartup_timeout=%s\nrequest_timeout=%s\n' \
  "$IMAGE" "$IMAGE_ID" "$ROUNDS" "$STARTUP_TIMEOUT" "$REQUEST_TIMEOUT" > "$REPORT/run.txt"
ACTIVE=""
VOLUME=""
cleanup() {
  local failed=0
  if [[ -n "$ACTIVE" ]]; then
    if docker rm --force "$ACTIVE" >/dev/null; then ACTIVE=""; else failed=1; fi
  fi
  if [[ -n "$VOLUME" ]]; then
    if docker volume rm "$VOLUME" >/dev/null; then VOLUME=""; else failed=1; fi
  fi
  if ((failed)); then
    echo "Cleanup failed. Owned resources: container=$ACTIVE volume=$VOLUME" >&2
    return 1
  fi
}
on_exit() {
  local code=$?
  trap - EXIT
  cleanup || code=2
  echo "Reports: $REPORT"
  exit "$code"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'echo "Runner failed; partial reports are preserved." >&2; exit 2' ERR
docker volume create --label warp-zapret.strategy-test=true "$RUN_ID" >/dev/null
VOLUME="$RUN_ID"

# Curl's response body (including trace/IP) stays in memory and is never printed.
probe() {
  local proxy="$1" url="$2" is_trace="$3" output code elapsed exit_code=0 body=""
  local args=(--disable --silent --proxy "$proxy" --noproxy ","
    --connect-timeout "$REQUEST_TIMEOUT" --max-time "$REQUEST_TIMEOUT"
    --proto '=https' --write-out $'\n%{http_code}\t%{time_total}')
  if [[ "$is_trace" == yes ]]; then
    args+=(--max-filesize 4096)
  else
    args+=(--head --output /dev/null)
  fi
  output="$(curl "${args[@]}" "$url" 2>/dev/null)" || exit_code=$?
  code="${output##*$'\n'}"
  elapsed="${code#*$'\t'}"
  code="${code%%$'\t'*}"
  [[ "$code" =~ ^[0-9]{3}$ ]] || code=000
  [[ "$elapsed" =~ ^[0-9]+([.][0-9]+)?$ ]] || elapsed=0
  PROBE_STATUS=FAIL
  if ((exit_code == 0)); then
    if [[ "$is_trace" == yes ]]; then
      body="${output%$'\n'*}"
      if [[ "$code" == 200 ]] && grep -Fxq 'warp=on' <<< "${body//$'\r'/}"; then
        PROBE_STATUS=PASS
      fi
    elif [[ "$code" =~ ^[23][0-9]{2}$ ]]; then
      PROBE_STATUS=PASS
    fi
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$strategy" "$round" "$kind" "$target" "$PROBE_STATUS" "$code" "$elapsed" "$exit_code" >> "$DETAILS"
}

WINNERS=()
TOTAL=$((ROUNDS * 2 * (${#TARGETS[@]} + 1)))
printf 'strategy\tstatus\tstartup_seconds\tpassed\ttotal\n'
for strategy in "${STRATEGIES[@]}"; do
  echo "Testing: $strategy" >&2
  passed=0
  status=STARTUP_FAIL
  started=$SECONDS
  if ACTIVE="$(docker create --name "$RUN_ID-$strategy" --init --restart no \
    --memory 256m --memory-swap 256m --cap-add NET_ADMIN --cap-add NET_RAW \
    --env MALLOC_TRIM_THRESHOLD_=1048576 --env MALLOC_MMAP_THRESHOLD_=1048576 \
    --device /dev/net/tun:/dev/net/tun \
    --publish 127.0.0.1::8888 --publish 127.0.0.1::1080 \
    --mount "type=volume,src=$VOLUME,dst=/var/lib/cloudflare-warp" \
    --tmpfs /tmp --tmpfs /run --no-healthcheck \
    "$IMAGE_ID" --strategy "$strategy")"; then
    if docker start "$ACTIVE" >/dev/null; then
      while ((SECONDS - started < STARTUP_TIMEOUT)); do
        running="$(docker inspect --format '{{.State.Running}}' "$ACTIVE")"
        [[ "$running" == true ]] || break
        if docker exec "$ACTIVE" /usr/bin/bash -c \
          'ss -lnt | grep -q ":8888 " && ss -lnt | grep -q ":1080 "' >/dev/null 2>&1; then
          status=READY
          break
        fi
        sleep 2
      done
    fi
  else
    ACTIVE=""
  fi
  startup=$((SECONDS - started))
  if [[ "$status" == READY ]]; then
    http="$(docker port "$ACTIVE" 8888/tcp)"
    socks="$(docker port "$ACTIVE" 1080/tcp)"
    [[ "$http" =~ ^127[.]0[.]0[.]1:[0-9]+$ && "$socks" =~ ^127[.]0[.]0[.]1:[0-9]+$ ]] ||
      die "Unexpected port bindings"
    for ((round=1; round<=ROUNDS; round++)); do
      for kind in HTTP SOCKS; do
        if [[ "$kind" == HTTP ]]; then proxy="http://$http"; else proxy="socks5h://$socks"; fi
        target=WARP
        probe "$proxy" https://www.cloudflare.com/cdn-cgi/trace yes
        if [[ "$PROBE_STATUS" == PASS ]]; then
          passed=$((passed + 1))
          for target in "${TARGETS[@]}"; do
            probe "$proxy" "$target" no
            [[ "$PROBE_STATUS" != PASS ]] || passed=$((passed + 1))
          done
        else
          for target in "${TARGETS[@]}"; do
            printf '%s\t%s\t%s\t%s\tSKIP\t-\t-\t-\n' \
              "$strategy" "$round" "$kind" "$target" >> "$DETAILS"
          done
        fi
      done
      ((round == ROUNDS)) || sleep 1
    done
    status=PARTIAL
    if ((passed == TOTAL)); then status=PASS; WINNERS+=("$strategy"); fi
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' "$strategy" "$status" "$startup" "$passed" "$TOTAL" | tee -a "$SUMMARY"
  if [[ -n "$ACTIVE" ]]; then
    docker stop --time 20 "$ACTIVE" >/dev/null
    docker rm "$ACTIVE" >/dev/null
    ACTIVE=""
  fi
done
if ((${#WINNERS[@]})); then
  echo "Fully passing strategies: ${WINNERS[*]}"
else
  echo "No strategy passed all checks."
  exit 1
fi
