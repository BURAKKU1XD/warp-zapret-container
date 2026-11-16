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
CONNECTIONS=2
DOWNLOAD_URL=""
DOWNLOAD_BYTES=""
DOWNLOAD_SHA256=""
UPLOAD_URL=""
OUTPUT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/test-results"
TARGETS_FILE=""
TARGETS=(
  https://discord.com/ https://gateway.discord.gg/ https://cdn.discordapp.com/
  https://updates.discord.com/ https://www.youtube.com/ https://youtu.be/
  https://i.ytimg.com/ https://redirector.googlevideo.com/
  https://www.google.com/ https://www.gstatic.com/
  https://www.cloudflare.com/ https://cdnjs.cloudflare.com/
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
  --connections N          Cold container starts per stage (default: 2)
  --download-url URL       Optional fixture with exact size and SHA256
  --download-bytes N       Expected fixture size (all three download options required)
  --download-sha256 HASH   Expected SHA256 of downloaded content
  --upload-url URL         Optional POST endpoint echoing exactly 65536 zero bytes
  --help
Order: direct host reference, WARP without zapret, selected strategies.
Reports distinguish availability, stability, and observed gain over the control.
Exit codes: 0 = at least one AVAILABLE strategy, 1 = none available,
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
    --image|--pull|--strategies|--rounds|--startup-timeout|--request-timeout|--targets-file|--output|--connections|--download-url|--download-bytes|--download-sha256|--upload-url)
      (($# >= 2)) && [[ -n "$2" && "$2" != --* ]] || die "$1 requires a value"
      case "$1" in
        --image) IMAGE=$2 ;; --pull) PULL=$2 ;; --strategies) SELECTED=$2 ;;
        --rounds) ROUNDS=$2 ;; --startup-timeout) STARTUP_TIMEOUT=$2 ;;
        --request-timeout) REQUEST_TIMEOUT=$2 ;; --targets-file) TARGETS_FILE=$2 ;;
        --output) OUTPUT=$2 ;; --connections) CONNECTIONS=$2 ;;
        --download-url) DOWNLOAD_URL=$2 ;; --download-bytes) DOWNLOAD_BYTES=$2 ;;
        --download-sha256) DOWNLOAD_SHA256=${2,,} ;; --upload-url) UPLOAD_URL=$2 ;;
      esac
      shift 2 ;;
    *) die "Unknown option: $1" ;;
  esac
done
[[ "$PULL" =~ ^(missing|always|never)$ ]] || die "Invalid --pull"
for value in "$ROUNDS" "$STARTUP_TIMEOUT" "$REQUEST_TIMEOUT" "$CONNECTIONS"; do
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
if [[ -n "$DOWNLOAD_URL$DOWNLOAD_BYTES$DOWNLOAD_SHA256" ]]; then
  [[ -n "$DOWNLOAD_URL" && "$DOWNLOAD_BYTES" =~ ^[1-9][0-9]{0,8}$ && "$DOWNLOAD_SHA256" =~ ^[0-9a-f]{64}$ ]] ||
    die "Download requires URL, positive byte count, and SHA256"
fi
for url in "${TARGETS[@]}" ${DOWNLOAD_URL:+"$DOWNLOAD_URL"} ${UPLOAD_URL:+"$UPLOAD_URL"}; do
  # No credentials, query strings, whitespace or control characters in reports.
  [[ "$url" =~ ^https://[a-zA-Z0-9.-]+(:[0-9]+)?(/[a-zA-Z0-9/._~-]*)?$ ]] ||
    die "Targets must be public HTTPS URLs without credentials or query strings"
done
for command in docker curl sha256sum head; do
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

# Refuse an old image rather than silently run an invalid no-zapret control.
HELP="$(docker run --rm --network none "$IMAGE_ID" --help)"
[[ "$HELP" == *--without-zapret* && "$HELP" == *--test-diagnostics* ]] ||
  die "Image lacks comparison support; pull a newer image or build this revision"
RUN_ID="warp-zapret-test-$(date -u +%Y%m%dT%H%M%S)-$$-$RANDOM"
REPORT="$OUTPUT/$RUN_ID"
mkdir -p -- "$OUTPUT"
mkdir -- "$REPORT"
SUMMARY="$REPORT/summary.tsv"
DETAILS="$REPORT/checks.tsv"
ATTEMPTS="$REPORT/connections.tsv"
HEADER=$'strategy\tstatus\tstartup_seconds\tpassed\ttotal\tstability\teffect\trecovered_checks\tqueue_packets\ttransport'
printf '%s\n' "$HEADER" > "$SUMMARY"
# Keep machine-readable TSV separate from the compact console presentation.
write_summary() {
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$@" >> "$SUMMARY"
  local display_stability="$6" display_effect="$7" display_transport="${10}"
  [[ "$display_stability" != NOT_APPLICABLE ]] || display_stability=-
  [[ "$display_effect" != NOT_APPLICABLE ]] || display_effect=-
  [[ "$display_transport" != NOT_APPLICABLE ]] || display_transport=-
  printf '\n%-22s  %-12s  %7s  %11s  %s\n' strategy status start_s checks stability
  printf '%-22s  %-12s  %7s  %11s  %s\n' "$1" "$2" "$3" "$4/$5" "$display_stability"
  printf '  effect=%s; recovered=%s; queue=%s; transport=%s\n\n' "$display_effect" "$8" "$9" "$display_transport"
}
printf 'strategy\tconnection\tround\tproxy\ttarget\tprotocol\tstatus\thttp_code\tseconds\tcurl_exit\tbytes\tttfb\n' > "$DETAILS"
printf 'strategy\tconnection\tstartup\tstartup_seconds\tpassed\ttotal\tnfqws_alive\ttcp_packets\tudp_packets\ttransport\n' > "$ATTEMPTS"
printf 'image=%s\nimage_id=%s\nrounds=%s\nconnections=%s\nstartup_timeout=%s\nrequest_timeout=%s\n' \
  "$IMAGE" "$IMAGE_ID" "$ROUNDS" "$CONNECTIONS" "$STARTUP_TIMEOUT" "$REQUEST_TIMEOUT" > "$REPORT/run.txt"
printf 'report_version=2\ndownload=%s\nupload=%s\nprofile_evidence=NOT_ESTABLISHED\nconfigured_transport=MASQUE_h3-with-h2-fallback\n' \
  "${DOWNLOAD_URL:-NOT_RUN}" "${UPLOAD_URL:-NOT_RUN}" >> "$REPORT/run.txt"
printf 'download_bytes=%s\ndownload_sha256=%s\n' "$DOWNLOAD_BYTES" "$DOWNLOAD_SHA256" >> "$REPORT/run.txt"
ACTIVE="" VOLUME=""
cleanup() {
  local failed=0
  if [[ -n "$ACTIVE" ]]; then
    if docker rm --force "$ACTIVE" >/dev/null; then ACTIVE=""; else failed=1; fi
  fi
  if [[ -n "$VOLUME" ]]; then
    if docker volume rm "$VOLUME" >/dev/null; then VOLUME=""; else failed=1; fi
  fi
  rm -f -- "$REPORT/body.tmp" "$REPORT/upload.tmp"
  return "$failed"
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
head -c 65536 /dev/zero > "$REPORT/upload.tmp"
UPLOAD_HASH="$(sha256sum < "$REPORT/upload.tmp")"; UPLOAD_HASH=${UPLOAD_HASH%% *}

declare -A CONTROL_GOOD CONTROL_BAD CURRENT_GOOD CURRENT_BAD
DIRECT_CLEAN=1
# Feed the literal bypass rule through stdin, without native wildcard expansion.
probe() {
  local proxy="$1" url="$2" type="$3" protocol="$4" output meta body="" digest=""
  exit_code=0
  local sink=/dev/null
  local route_args=(--proxy "$proxy" --noproxy ",")
  [[ "$kind" != DIRECT ]] || route_args=(--proxy "$proxy" --config -)
  local args=(--disable --silent "${route_args[@]}"
    --connect-timeout "$REQUEST_TIMEOUT" --max-time "$REQUEST_TIMEOUT"
    --proto '=https' --proto-redir '=https' --location --max-redirs 3
    --write-out $'\n%{http_code}\t%{time_total}\t%{size_download}\t%{time_starttransfer}')
  case "$protocol" in
    HTTP1.1) args+=(--http1.1) ;;
    TLS1.2) args+=(--tlsv1.2 --tls-max 1.2) ;;
    TLS1.3) args+=(--tlsv1.3 --tls-max 1.3) ;;
  esac
  case "$type" in
    trace) args+=(--max-filesize 4096) ;;
    download|upload)
      sink="$REPORT/body.tmp"
      args+=(--max-filesize "${DOWNLOAD_BYTES:-65536}")
      if [[ "$type" == upload ]]; then
        args+=(--max-filesize 65536 --data-binary "@$REPORT/upload.tmp")
      fi
      args+=(--output "$sink") ;;
    *) args+=(--output "$sink") ;;
  esac
  if [[ "$kind" == DIRECT ]]; then
    output="$(printf '%s\n' 'noproxy = "*"' | curl "${args[@]}" "$url" 2>/dev/null)" || exit_code=$?
  else
    output="$(curl "${args[@]}" "$url" 2>/dev/null)" || exit_code=$?
  fi
  meta="${output##*$'\n'}"
  meta="${meta%$'\r'}"
  IFS=$'\t' read -r code elapsed bytes ttfb <<< "$meta"
  [[ "$code" =~ ^[0-9]{3}$ ]] || code=000
  [[ "$elapsed" =~ ^[0-9]+([.][0-9]+)?$ ]] || elapsed=0
  [[ "$bytes" =~ ^[0-9]+$ ]] || bytes=0
  [[ "$ttfb" =~ ^[0-9]+([.][0-9]+)?$ ]] || ttfb=0
  PROBE_STATUS=FAIL
  if ((exit_code == 0)) && [[ "$code" =~ ^2[0-9]{2}$ ]]; then
    PROBE_STATUS=PASS
    if [[ "$type" == trace ]]; then
      body="${output%$'\n'*}"
      if [[ "$kind" == DIRECT ]]; then
        if grep -Fxq 'warp=on' <<< "${body//$'\r'/}"; then
          DIRECT_CLEAN=0; PROBE_STATUS=CONTAMINATED
        elif ! grep -Fxq 'warp=off' <<< "${body//$'\r'/}"; then PROBE_STATUS=FAIL; fi
      elif ! grep -Fxq 'warp=on' <<< "${body//$'\r'/}"; then PROBE_STATUS=FAIL; fi
      [[ "$code" == 200 ]] || PROBE_STATUS=FAIL
    elif [[ "$type" == download || "$type" == upload ]]; then
      digest="$(sha256sum < "$sink")"; digest=${digest%% *}
      if [[ "$type" == download ]]; then
        [[ "$bytes" == "$DOWNLOAD_BYTES" && "$digest" == "$DOWNLOAD_SHA256" ]] || PROBE_STATUS=FAIL
      else
        [[ "$bytes" == 65536 && "$digest" == "$UPLOAD_HASH" ]] || PROBE_STATUS=FAIL
      fi
    fi
  elif [[ "$exit_code" == 4 ]]; then PROBE_STATUS=UNSUPPORTED
  elif [[ "$exit_code" == 35 || "$exit_code" == 60 ]]; then PROBE_STATUS=TLS_ERROR
  elif [[ "$exit_code" == 28 ]]; then PROBE_STATUS=TIMEOUT
  elif [[ "$code" =~ ^[45][0-9]{2}$ ]]; then PROBE_STATUS=HTTP_ERROR
  fi
  if [[ "$target" == GUARD || "$target" == STOPPED_PROXY ]]; then
    PROBE_STATUS=GUARD_FAIL
    if [[ "$code" == 000 && "$exit_code" == 7 ]]; then PROBE_STATUS=EXPECTED_REFUSAL; fi
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$strategy" "$connection" "$round" "$kind" "$target" "$protocol" "$PROBE_STATUS" \
    "$code" "$elapsed" "$exit_code" "$bytes" "$ttfb" >> "$DETAILS"
  if [[ "$PROBE_STATUS" == PASS ]]; then
    passed=$((passed + 1)); connection_passed=$((connection_passed + 1))
  fi
  if [[ "$kind" == DIRECT && "$type" == trace && "$PROBE_STATUS" != PASS ]]; then DIRECT_CLEAN=0; fi
  local key="$kind|$target|$protocol"
  [[ "$target" != GUARD && "$target" != STOPPED_PROXY ]] || return 0
  if [[ "$strategy" == warp-control ]]; then
    if [[ "$PROBE_STATUS" == PASS ]]; then CONTROL_GOOD[$key]=1
    elif [[ "$PROBE_STATUS" != UNSUPPORTED ]]; then CONTROL_BAD[$key]=1; fi
  elif [[ "$strategy" != direct ]]; then
    if [[ "$PROBE_STATUS" == PASS ]]; then CURRENT_GOOD[$key]=1
    else CURRENT_BAD[$key]=1; fi
  fi
}
run_requests() {
  local proxy="$1" protocol
  for ((round=1; round<=ROUNDS; round++)); do
    echo "  $strategy connection=$connection round=$round proxy=$kind" >&2
    target=WARP
    probe "$proxy" https://www.cloudflare.com/cdn-cgi/trace trace DEFAULT
    # Run sites even if trace failed: failures must remain visible individually.
    for target in "${TARGETS[@]}"; do
      for protocol in HTTP1.1 TLS1.2 TLS1.3; do probe "$proxy" "$target" site "$protocol"; done
    done
    if [[ -n "$DOWNLOAD_URL" ]]; then target=DOWNLOAD; probe "$proxy" "$DOWNLOAD_URL" download HTTP1.1; fi
    if [[ -n "$UPLOAD_URL" ]]; then target=UPLOAD; probe "$proxy" "$UPLOAD_URL" upload HTTP1.1; fi
    ((round == ROUNDS)) || sleep 1
  done
}
PER_ROUTE=$((ROUNDS * (1 + 3 * ${#TARGETS[@]} + ( ${#DOWNLOAD_URL} > 0 ) + ( ${#UPLOAD_URL} > 0 ))))
# A refused loopback port must fail even with NO_PROXY=*; no external DNS needed.
strategy=proxy-guard connection=0 round=0 target=GUARD passed=0 connection_passed=0
for kind in HTTP SOCKS; do
  if [[ "$kind" == HTTP ]]; then proxy=http://127.0.0.1:1; else proxy=socks5h://127.0.0.1:1; fi
  probe "$proxy" https://example.invalid/ site HTTP1.1
  [[ "$code" == 000 && "$exit_code" == 7 ]] || die "Closed proxy guard failed for $kind"
done
strategy=direct connection=1 kind=DIRECT passed=0 connection_passed=0
run_requests http://127.0.0.1:1
write_summary direct REFERENCE 0 "$passed" "$PER_ROUTE" NOT_APPLICABLE NOT_APPLICABLE 0 0 NOT_APPLICABLE
printf 'direct_reference_clean=%s\nhost_route_isolation=NOT_ESTABLISHED\n' "$DIRECT_CLEAN" >> "$REPORT/run.txt"

docker volume create --label warp-zapret.strategy-test=true "$RUN_ID" >/dev/null
VOLUME="$RUN_ID"
available=0
for strategy in warp-control "${STRATEGIES[@]}"; do
  echo "Testing: $strategy ($CONNECTIONS cold connections)" >&2
  passed=0 startup=0 ready_count=0 queue_packets=0 queue_known=1 alive_all=1
  CURRENT_GOOD=() CURRENT_BAD=()
  for ((connection=1; connection<=CONNECTIONS; connection++)); do
    status=STARTUP_FAIL connection_passed=0 alive=0 tcp=0 udp=0 transport=UNKNOWN
    started=$SECONDS
    selection=(--strategy "$strategy")
    [[ "$strategy" != warp-control ]] || selection=(--strategy general --without-zapret)
    if ACTIVE="$(docker create --name "$RUN_ID-$strategy-$connection" --init --restart no \
      --memory 256m --memory-swap 256m --cap-add NET_ADMIN --cap-add NET_RAW \
      --env MALLOC_TRIM_THRESHOLD_=1048576 --env MALLOC_MMAP_THRESHOLD_=1048576 \
      --env HTTP_PROXY= --env HTTPS_PROXY= --env ALL_PROXY= --env FTP_PROXY= --env NO_PROXY= \
      --env http_proxy= --env https_proxy= --env all_proxy= --env ftp_proxy= --env no_proxy= \
      --device /dev/net/tun:/dev/net/tun --publish 127.0.0.1::8888 --publish 127.0.0.1::1080 \
      --mount "type=volume,src=$VOLUME,dst=/var/lib/cloudflare-warp" \
      --tmpfs /tmp --tmpfs /run --no-healthcheck "$IMAGE_ID" "${selection[@]}")"; then
      if docker start "$ACTIVE" >/dev/null; then
        while ((SECONDS - started < STARTUP_TIMEOUT)); do
          [[ "$(docker inspect --format '{{.State.Running}}' "$ACTIVE")" == true ]] || break
          if docker exec "$ACTIVE" /usr/bin/bash -c \
            'test -f /run/warp-zapret.ready && ss -lnt | grep -q ":8888 " && ss -lnt | grep -q ":1080 "' >/dev/null 2>&1; then
            status=READY; break
          fi
          sleep 2
        done
      fi
    else ACTIVE=""; fi
    connection_startup=$((SECONDS - started)); startup=$((startup + connection_startup))
    if [[ "$status" == READY ]]; then
      ready_count=$((ready_count + 1))
      http="$(docker port "$ACTIVE" 8888/tcp)"; socks="$(docker port "$ACTIVE" 1080/tcp)"
      [[ "$http" =~ ^127[.]0[.]0[.]1:[0-9]+$ && "$socks" =~ ^127[.]0[.]0[.]1:[0-9]+$ ]] || die "Unexpected port bindings"
      for kind in HTTP SOCKS; do
        if [[ "$kind" == HTTP ]]; then proxy="http://$http"; else proxy="socks5h://$socks"; fi
        run_requests "$proxy"
      done
      if diagnostic="$(docker exec "$ACTIVE" /entrypoint.sh --test-diagnostics 2>/dev/null)" &&
        [[ "$diagnostic" =~ ^[01]$'\t'[0-9]+$'\t'[0-9]+$'\t'UNKNOWN$ ]]; then
        IFS=$'\t' read -r alive tcp udp transport <<< "$diagnostic"
        if [[ "$strategy" == warp-control ]] && ((alive != 0 || tcp + udp != 0)); then
          die "No-zapret control unexpectedly has nfqws or queued packets"
        fi
        queue_packets=$((queue_packets + tcp + udp))
      else queue_known=0; fi
    fi
    [[ "$alive" == 1 ]] || alive_all=0
    if [[ -n "$ACTIVE" ]]; then
      # Verify that the very proxy which worked cannot work after container stop.
      docker stop --time 20 "$ACTIVE" >/dev/null
      if [[ "$status" == READY ]]; then
        for kind in HTTP SOCKS; do
          target=STOPPED_PROXY round=0
          previous_passed=$passed; previous_connection_passed=$connection_passed
          if [[ "$kind" == HTTP ]]; then proxy="http://$http"; else proxy="socks5h://$socks"; fi
          probe "$proxy" https://example.invalid/ site HTTP1.1
          passed=$previous_passed; connection_passed=$previous_connection_passed
          [[ "$code" == 000 && "$exit_code" == 7 ]] || die "Stopped container proxy guard failed for $kind"
        done
      fi
      docker rm "$ACTIVE" >/dev/null; ACTIVE=""
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$strategy" "$connection" "$status" \
      "$connection_startup" "$connection_passed" "$((PER_ROUTE * 2))" "$alive" "$tcp" "$udp" "$transport" >> "$ATTEMPTS"
  done
  total=$((CONNECTIONS * 2 * PER_ROUTE))
  status=PARTIAL stability=FAIL effect=NOT_ESTABLISHED recovered=0
  ((ready_count > 0)) || status=STARTUP_FAIL
  if ((passed == total)); then
    status=AVAILABLE
    if ((CONNECTIONS > 1 && ROUNDS > 1)); then stability=PASS; else stability=INSUFFICIENT_SAMPLES; fi
  fi
  if [[ "$strategy" != warp-control ]]; then
    for key in "${!CURRENT_GOOD[@]}"; do
      if [[ -n "${CONTROL_BAD[$key]:-}" && -z "${CONTROL_GOOD[$key]:-}" && -z "${CURRENT_BAD[$key]:-}" ]]; then
        recovered=$((recovered + 1))
      fi
    done
    if ((control_passed == total)); then effect=WARP_WORKS_WITHOUT_ZAPRET
    elif ((recovered > 0 && queue_packets > 0 && queue_known == 1 && alive_all == 1 && DIRECT_CLEAN == 1 && control_valid == 1 && ready_count == CONNECTIONS)); then effect=OBSERVED_GAIN; fi
    [[ "$status" != AVAILABLE ]] || available=$((available + 1))
  else
    control_passed=$passed; effect=CONTROL; control_valid=0
    if ((ready_count == CONNECTIONS && queue_known == 1 && queue_packets == 0 && alive_all == 0)); then control_valid=1; fi
  fi
  ((queue_known == 1)) || queue_packets=UNKNOWN
  write_summary "$strategy" "$status" "$startup" "$passed" "$total" \
    "$stability" "$effect" "$recovered" "$queue_packets" UNKNOWN
done
echo 'AVAILABLE measures connectivity. OBSERVED_GAIN is a comparison, not proof of a particular desync profile.'
((available > 0)) || exit 1
