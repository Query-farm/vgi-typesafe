#!/usr/bin/env bash
#
# Run test/sql/*.test through DuckDB (the haybarn-unittest sqllogictest runner)
# against this worker, over one transport.
#
#   WORKER=/abs/path/to/worker-executable TRANSPORT=stdio ci/run-integration.sh
#
# Required environment:
#   WORKER            absolute path to an executable that serves the worker:
#                     no arguments = stdio; `--http --port 0` = HTTP, printing
#                     `PORT:<n>` on stdout once listening.
# Optional:
#   TRANSPORT         stdio | launch | http                        (default: stdio)
#   HAYBARN_UNITTEST  path to haybarn-unittest   (default: the one on PATH)
#   PYTHON            command for the mock API, may contain spaces (default: python3)
#   MIN_EXECUTED      fail if fewer test cases ran (default: 1)
#   EXPECTED_SKIP_REASONS  newline-separated skip reasons that are allowed,
#                     e.g. "require-env TYPESAFE_LIVE". Any other skip fails.
#   RECIPES_WORKER    command that runs a recipe by name (`$RECIPES_WORKER <recipe>`);
#                     when set, examples/test/*.test run too (stdio/launch only)
#   WARMUP_TIMEOUT    seconds allowed for the extension install (default: 300)
#   KEEP_STAGE=1      keep the staged test tree for debugging
#
# Transports:
#   stdio   DuckDB spawns the worker and talks over stdin/stdout (the default).
#   launch  DuckDB starts the worker once on an AF_UNIX socket and pools
#           connections (`LOCATION 'launch:/path'`); best for slow-starting
#           runtimes such as the JVM.
#   http    the worker runs as an HTTP server (`--http`), as in Docker deploys.
#
# Why the extra checks: the sqllogictest runner turns some errors into silent
# SKIPs (anything mentioning "HTTP" without --test-config) and exits 0 when no
# test matched. Both would make a broken lane look green, so this script passes
# a config that disables error-skipping and fails on any unexpected skip.
set -euo pipefail

: "${WORKER:?absolute path to the worker executable}"
TRANSPORT="${TRANSPORT:-stdio}"
HAYBARN_UNITTEST="${HAYBARN_UNITTEST:-$(command -v haybarn-unittest || true)}"
PYTHON="${PYTHON:-python3}"
MIN_EXECUTED="${MIN_EXECUTED:-1}"
[ -n "$HAYBARN_UNITTEST" ] || { echo "::error::haybarn-unittest not found (make tools)"; exit 1; }

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
STAGE="$(mktemp -d)"
PIDS=()
cleanup() {
  for p in ${PIDS[@]+"${PIDS[@]}"}; do kill "$p" 2>/dev/null || true; done
  [ "${KEEP_STAGE:-0}" = "1" ] && echo "stage kept at $STAGE" || rm -rf "$STAGE"
}
trap cleanup EXIT

# start_server <log> <cmd...> — run a server that prints PORT:<n>; sets PORT.
PORT=""
start_server() {
  local log="$1"; shift
  # Create the log before backgrounding: the child opens its redirect only
  # once it is scheduled, and the poll below would otherwise read a missing
  # file, which under set -e -o pipefail kills the whole run.
  : > "$log"
  "$@" >"$log" 2>&1 &
  local pid=$!
  PIDS+=("$pid")
  for _ in $(seq 1 120); do
    kill -0 "$pid" 2>/dev/null || { echo "::error::'$*' exited early:"; cat "$log"; return 1; }
    PORT="$(sed -n 's/^PORT:\([0-9][0-9]*\).*/\1/p' "$log" | head -1)"
    [ -n "$PORT" ] && return 0
    sleep 0.5
  done
  echo "::error::'$*' never printed PORT:<n>"; cat "$log"; return 1
}

# The bundled mock TypeSafe endpoint (test/mock_api/server.py, requiring the key
# 'test-key'). No real key may leak in: the worker falls back to these variables
# when no secret resolves, and the auth tests assert what happens without one.
unset TYPESAFE_API_KEY TYPESAFE_BASE_URL
start_server "$STAGE/mock.log" $PYTHON "$REPO/test/mock_api/server.py" --api-key test-key
export TYPESAFE_MOCK_URL="http://127.0.0.1:$PORT"

HTTP=0
case "$TRANSPORT" in
  stdio)  export VGI_TYPESAFE_WORKER="$WORKER" ;;
  launch) export VGI_TYPESAFE_WORKER="launch:$WORKER" ;;
  http)
    HTTP=1
    start_server "$STAGE/worker.log" "$WORKER" --http --port 0
    export VGI_TYPESAFE_WORKER="http://127.0.0.1:$PORT"
    ;;
  *) echo "::error::unknown TRANSPORT=$TRANSPORT (expected stdio|launch|http)"; exit 1 ;;
esac

# Stage the tests, rewriting `require vgi` into a signed INSTALL + LOAD (the
# standalone runner links no extensions) and loading httpfs for HTTP workers.
mkdir -p "$STAGE/test/sql"
TEST_FILES=("$REPO"/test/sql/*.test)
if [ -n "${RECIPES_WORKER:-}" ] && [ "$HTTP" = "0" ]; then
  TEST_FILES+=("$REPO"/examples/test/*.test)
fi
for f in "${TEST_FILES[@]}"; do
  awk -v http="$HTTP" '
    /^require[ \t]+vgi[ \t]*$/ {
      print "statement ok"; print "LOAD vgi;"; print ""
      if (http == 1) { print "statement ok"; print "LOAD httpfs;"; print "" }
      next
    }
    { print }' "$f" > "$STAGE/test/sql/$(basename "$f")"
done

# Install the extensions once, refreshing any stale cached copy of vgi.
cat > "$STAGE/test/_warm.test" <<'EOF'
statement ok
FORCE INSTALL vgi FROM community;

statement ok
INSTALL httpfs FROM core;

statement ok
INSTALL json FROM core;
EOF
# Bounded: a stalled extension download would otherwise hang the lane until the
# job timeout. WARMUP_TIMEOUT is in seconds.
"$HAYBARN_UNITTEST" --keep-home --test-dir "$STAGE" "test/_warm.test" >/dev/null 2>&1 &
warm_pid=$!
warm_deadline=$((SECONDS + ${WARMUP_TIMEOUT:-300}))
while kill -0 "$warm_pid" 2>/dev/null && [ "$SECONDS" -lt "$warm_deadline" ]; do sleep 1; done
if kill -0 "$warm_pid" 2>/dev/null; then
  kill "$warm_pid" 2>/dev/null || true
  wait "$warm_pid" 2>/dev/null || true
  echo "::warning::extension warm-up timed out after ${WARMUP_TIMEOUT:-300}s (stalled download?)"
elif ! wait "$warm_pid"; then
  echo "::warning::extension warm-up did not fully succeed"
fi
rm -f "$STAGE/test/_warm.test"

cat > "$STAGE/no_error_skip.json" <<'EOF'
{"skip_error_messages": ["__vgi_starter_never_matches__"]}
EOF

echo "Running test/sql over $TRANSPORT (worker: $VGI_TYPESAFE_WORKER)"
LOG="$STAGE/unittest.log"
rc=0
( cd "$STAGE" && "$HAYBARN_UNITTEST" --keep-home --test-dir "$STAGE" \
    --test-config "$STAGE/no_error_skip.json" "test/sql/*" ) 2>&1 | tee "$LOG" && rc=0 || rc="${PIPESTATUS[0]}"

if grep -q 'No test cases matched\|No tests ran' "$LOG"; then
  echo "::error::no test cases matched; the staging is wrong"; exit 1
fi
total="$(sed -n 's/^\[[0-9]*\/\([0-9]*\)\].*/\1/p' "$LOG" | tail -1)"
total="${total:-0}"
skips="$(awk '/^Skipped tests for the following reasons:/{b=1;next} b&&/^[[:space:]]*$/{b=0} b&&match($0,/: [0-9]+[[:space:]]*$/){print substr($0,1,RSTART-1) "\t" substr($0,RSTART+2)+0}' "$LOG")"
skipped=0
while IFS=$'\t' read -r reason n; do
  [ -n "$reason" ] || continue
  skipped=$((skipped + n))
  if ! printf '%s\n' "${EXPECTED_SKIP_REASONS:-}" | grep -qxF "$reason"; then
    echo "::error::unexpected skip: '$reason'. A test file silently stopped running;" \
         "fix the gate or add it to EXPECTED_SKIP_REASONS."
    rc=1
  fi
done <<< "$skips"
executed=$((total - skipped))
echo "executed $executed of $total test files over $TRANSPORT ($skipped skipped)"
if [ "$executed" -lt "$MIN_EXECUTED" ]; then
  echo "::error::only $executed test files ran (MIN_EXECUTED=$MIN_EXECUTED)"; rc=1
fi
if [ "$HTTP" = "1" ] && ! kill -0 "${PIDS[1]}" 2>/dev/null; then
  echo "::error::the HTTP worker died during the run:"; cat "$STAGE/worker.log"; rc=1
fi
exit "$rc"
