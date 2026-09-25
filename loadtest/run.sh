#!/usr/bin/env bash
# Run a k6 scenario in Docker against the local stack, with a RAM guard for small laptops.
#   loadtest/run.sh <label> <env-file> [extra k6 args...]
# Writes loadtest/results/<label>.json (k6 summary) and <label>.txt (console output).
set -euo pipefail
label=$1; envfile=$2; shift 2
min_avail=${LOADTEST_MIN_AVAIL_MB:-1500}
cd "$(dirname "$0")/.."
avail=$(free -m | awk '/^Mem:/{print $7}')
if [ "$avail" -lt 3000 ]; then echo "only ${avail} MB available (< 3000), not starting"; exit 3; fi
envargs=(); while IFS= read -r line; do [ -n "$line" ] && envargs+=(-e "$line"); done < "$envfile"
# RAM guard: kill k6 if available memory drops below the floor.
( while docker inspect k6run >/dev/null 2>&1 || sleep 2; do
    a=$(free -m | awk '/^Mem:/{print $7}')
    if [ "$a" -lt "$min_avail" ]; then echo "RAM guard: ${a} MB available, killing k6" >&2; docker kill k6run >/dev/null 2>&1; break; fi
    docker inspect k6run >/dev/null 2>&1 || break
    sleep 2
  done ) &
guard=$!
docker run --rm --name k6run --user "$(id -u):$(id -g)" --network host -v "$PWD/loadtest:/lt" "${envargs[@]}" "$@" \
  grafana/k6 run --summary-export "/lt/results/${label}.json" /lt/k6/booking_rush.js \
  2>&1 | tee "loadtest/results/${label}.txt"
kill "$guard" 2>/dev/null || true
