#!/usr/bin/env bash
# End-to-end verification of the shared byte-rate cap of the two independent
# /pool/ servers (pool-a, pool-b), with packages published through the
# original entry (apt-repo) and downloads from two client containers (two
# source IPs).
#
# Phases:
#   P0  one source IP, one connection        -> may use the full cap
#   P1  one source IP, two connections, one  -> still one share total
#       to each process (cross-process)
#   P2  two source IPs concurrently          -> each gets half the cap
#   P3  SIGKILL pool-a mid-download          -> pool-b's in-flight download
#       continues and uses the freed share within 2 s; cap holds throughout
# Every successful download is checked against the SHA-256 published in the
# repository's Packages index, and the aggregate byte stream is checked
# against "rate * duration + 64 KiB" for every time window.
#
# Usage: scripts/verify-shared-limit.sh
#   Env: APT_REPO_RATE_BPS (default 2097152 = 2 MiB/s), KEEP=1 to keep the
#   compose project running, VERIFY_PROJECT to override the project name.
#
# Requires: docker compose v2, curl, python3, awk.  Uses its own compose
# project (default name aptrepo-limit-verify) and ports 18080-18082, so it
# can run next to a normal deployment; it publishes test packages into that
# project's private data volume and tears everything down on exit.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${VERIFY_PROJECT:-aptrepo-limit-verify}"
export APT_REPO_PORT="${APT_REPO_PORT:-18080}"
export APT_POOL_A_PORT="${APT_POOL_A_PORT:-18081}"
export APT_POOL_B_PORT="${APT_POOL_B_PORT:-18082}"
export APT_REPO_RATE_BPS="${APT_REPO_RATE_BPS:-2097152}"
export APT_REPO_RATE_BURST="${APT_REPO_RATE_BURST:-65536}"
RATE="$APT_REPO_RATE_BPS"
BURST="$APT_REPO_RATE_BURST"
COMPOSE=(docker compose -p "$PROJECT" -f docker-compose.yml -f docker-compose.verify.yml)
RESULTS="$PWD/.verify-results"
KEEP="${KEEP:-0}"

WORKDIR="$(mktemp -d)"
cleanup() {
    if [ "$KEEP" != "1" ]; then
        "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    fi
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
rm -rf "$RESULTS"
mkdir -p "$RESULTS"
chmod 777 "$RESULTS"   # client containers write here as uid 10001

frac() { awk -v r="$RATE" -v f="$1" 'BEGIN{printf "%.0f", r*f}'; }
nowf() { date +%s.%N; }
plus() { awk -v a="$1" -v b="$2" 'BEGIN{printf "%.6f", a+b}'; }

wait_url() { # url name
    local url="$1" name="$2" n=0
    until curl -fsS -o /dev/null "$url"; do
        n=$((n+1)); [ "$n" -gt 60 ] && { echo "FAIL: $name did not come up" >&2; exit 1; }
        sleep 1
    done
    echo "== $name is up ($url)"
}

analyze() { python3 -I tests/analyze_limit.py "$@"; }

run_jobs() { # client prefix extra-loadgen-args...
    local client="$1" prefix="$2"
    shift 2
    "${COMPOSE[@]}" exec -T "$client" \
        python3 -I /tests/loadgen.py --out-dir /results --prefix "$prefix" "$@"
}

echo "== build image"
"${COMPOSE[@]}" build -q apt-repo

echo "== start the original publishing entry"
"${COMPOSE[@]}" up -d apt-repo >/dev/null
wait_url "http://127.0.0.1:${APT_REPO_PORT}/api/status" "apt-repo"

echo "== build test packages (8 MiB, 8 MiB, 16 MiB payloads)"
python3 -I tests/debbuild.py --out "$WORKDIR/limit-one_1.0.0_amd64.deb"   --package limit-one   --size 8M  --seed 11 >/dev/null
python3 -I tests/debbuild.py --out "$WORKDIR/limit-two_1.0.0_amd64.deb"   --package limit-two   --size 8M  --seed 22 >/dev/null
python3 -I tests/debbuild.py --out "$WORKDIR/limit-three_1.0.0_amd64.deb" --package limit-three --size 16M --seed 33 >/dev/null

echo "== publish via the original entry (POST /api/publish)"
curl -fsS -X POST "http://127.0.0.1:${APT_REPO_PORT}/api/publish" \
    -F files=@"$WORKDIR/limit-one_1.0.0_amd64.deb" \
    -F files=@"$WORKDIR/limit-two_1.0.0_amd64.deb" \
    -F files=@"$WORKDIR/limit-three_1.0.0_amd64.deb" \
    -o "$WORKDIR/publish.json"
python3 -c "import json; d=json.load(open('$WORKDIR/publish.json')); print('   publication', d['published']['id'], 'with', d['published']['package_count'], 'packages')"

curl -fsS "http://127.0.0.1:${APT_REPO_PORT}/dists/stable/main/binary-amd64/Packages" -o "$WORKDIR/Packages"
path_of() { awk -v pkg="$1" '/^Package: /{p=($2==pkg)} p && /^Filename: /{print $2; exit}' "$WORKDIR/Packages"; }
sha_of()  { awk -v f="$1" '/^Filename: /{hit=($2==f)} hit && /^SHA256: /{print $2; exit}' "$WORKDIR/Packages"; }
size_of() { awk -v f="$1" '/^Filename: /{hit=($2==f)} hit && /^Size: /{print $2; exit}' "$WORKDIR/Packages"; }
ONE=$(path_of limit-one);   TWO=$(path_of limit-two);   THREE=$(path_of limit-three)
ONE_SHA=$(sha_of "$ONE");   TWO_SHA=$(sha_of "$TWO");   THREE_SHA=$(sha_of "$THREE")
ONE_SIZE=$(size_of "$ONE"); TWO_SIZE=$(size_of "$TWO"); THREE_SIZE=$(size_of "$THREE")
echo "   $ONE ($ONE_SIZE B)"
echo "   $TWO ($TWO_SIZE B)"
echo "   $THREE ($THREE_SIZE B)"

echo "== start the two independent /pool/ servers and the two clients"
"${COMPOSE[@]}" up -d pool-a pool-b client1 client2 >/dev/null
wait_url "http://127.0.0.1:${APT_POOL_A_PORT}/healthz" "pool-a (direct)"
wait_url "http://127.0.0.1:${APT_POOL_B_PORT}/healthz" "pool-b (direct)"

echo "== P0: single source IP, single connection -> full cap ($RATE B/s)"
run_jobs client1 p0 --job "one=http://pool-a:8081/$ONE"
analyze rate --min-rate "$(frac 0.85)" --max-rate "$(frac 1.15)" "$RESULTS/p0.one.samples.jsonl"
analyze hash --summary "$RESULTS/p0.one.json" --sha256 "$ONE_SHA" --size "$ONE_SIZE"

echo "== P1: same source IP, two connections across BOTH processes -> one share"
run_jobs client1 p1 \
    --job "a=http://pool-a:8081/$ONE" \
    --job "b=http://pool-b:8081/$TWO"
analyze rate --min-rate "$(frac 0.8)" --max-rate "$(frac 1.2)" \
    "$RESULTS/p1.a.samples.jsonl" "$RESULTS/p1.b.samples.jsonl"
analyze bound --rate "$RATE" --burst "$BURST" \
    "$RESULTS/p1.a.samples.jsonl" "$RESULTS/p1.b.samples.jsonl"
analyze hash --summary "$RESULTS/p1.a.json" --sha256 "$ONE_SHA" --size "$ONE_SIZE"
analyze hash --summary "$RESULTS/p1.b.json" --sha256 "$TWO_SHA" --size "$TWO_SIZE"

echo "== P2: two source IPs concurrently -> half the cap each"
run_jobs client1 p2c1 --job "one=http://pool-a:8081/$ONE" &
run_jobs client2 p2c2 --job "two=http://pool-b:8081/$TWO" &
wait
analyze rate --min-rate "$(frac 0.35)" --max-rate "$(frac 0.65)" "$RESULTS/p2c1.one.samples.jsonl"
analyze rate --min-rate "$(frac 0.35)" --max-rate "$(frac 0.65)" "$RESULTS/p2c2.two.samples.jsonl"
analyze rate --min-rate "$(frac 0.8)" --max-rate "$(frac 1.2)" \
    "$RESULTS/p2c1.one.samples.jsonl" "$RESULTS/p2c2.two.samples.jsonl"
analyze bound --rate "$RATE" --burst "$BURST" \
    "$RESULTS/p2c1.one.samples.jsonl" "$RESULTS/p2c2.two.samples.jsonl"
analyze hash --summary "$RESULTS/p2c1.one.json" --sha256 "$ONE_SHA" --size "$ONE_SIZE"
analyze hash --summary "$RESULTS/p2c2.two.json" --sha256 "$TWO_SHA" --size "$TWO_SIZE"

echo "== P3: SIGKILL pool-a mid-download -> pool-b continues, freed share within 2 s"
run_jobs client1 p3c1 --job "three=http://pool-b:8081/$THREE" &
run_jobs client2 p3c2 --job "two=http://pool-a:8081/$TWO" &
sleep 4
KILL_AT="$(nowf)"
"${COMPOSE[@]}" kill -s SIGKILL pool-a
echo "   pool-a killed at $KILL_AT"
wait
# In-flight download on the surviving process completed intact:
analyze hash --summary "$RESULTS/p3c1.three.json" --sha256 "$THREE_SHA" --size "$THREE_SIZE"
# The download served by the killed process was cut short:
analyze incomplete --summary "$RESULTS/p3c2.two.json"
# Before the kill the surviving download held half the cap:
analyze rate --t0 "$(plus "$KILL_AT" -3.0)" --t1 "$(plus "$KILL_AT" -1.0)" \
    --min-rate "$(frac 0.3)" --max-rate "$(frac 0.7)" "$RESULTS/p3c1.three.samples.jsonl"
# Within 2 s of the kill it owned the freed share (full cap):
analyze rate --t0 "$(plus "$KILL_AT" 2.0)" --t1 "$(plus "$KILL_AT" 5.0)" \
    --min-rate "$(frac 0.8)" "$RESULTS/p3c1.three.samples.jsonl"
# The shared cap held throughout, including across the kill:
analyze bound --rate "$RATE" --burst "$BURST" \
    "$RESULTS/p3c1.three.samples.jsonl" "$RESULTS/p3c2.two.samples.jsonl"

echo "== global byte bound over all phases"
# shellcheck disable=SC2086
analyze bound --rate "$RATE" --burst "$BURST" "$RESULTS"/p*.samples.jsonl

echo "== OK: shared cap, per-IP fairness, kill behaviour and SHA-256 all verified"
