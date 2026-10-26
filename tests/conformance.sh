#!/usr/bin/env bash
# Run the GA4GH TES conformance suite (ga4gh/openapi-test-runner) against a
# running TESP-API. Writes .conformance/report/: service-info.json, exit-code,
# and per TES version report.json, run.log and summary.md.
#
# Usage: bash tests/conformance.sh   (Linux, GNU sed)
#
# Environment (all optional):
#   TES_URL        server root, without the /v1 suffix (default http://localhost:8080/)
#   TES_VERSIONS   space-separated TES versions to test (default "1.0.0 1.1.0")
#   WORK_DIR       where the runner, its virtualenv and the reports go (default <repo>/.conformance)
#   POLL_TIMEOUT   seconds a test waits for a task to reach a terminal state (default 600;
#                  the suite ships 3600, which turns one stuck task into an hour-long run)
#   RUN_TIMEOUT    seconds one TES version's run may take in total (default 1500)
#
# Exits 0 when every test passes, 1 when any test fails, 2 when the run breaks or times out.

set -euo pipefail

# Pinned: the runner has no tags or releases. To bump, change RUNNER_SHA, check that
# the timeout patch below still applies and that the log line matched in
# conformance_summary.py is unchanged, then compare summary.md before and after.
RUNNER_REPO="https://github.com/ga4gh/openapi-test-runner.git"
RUNNER_SHA="bc3eb911e877521409348e0225852d072d4de196"

TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
TES_URL="${TES_URL:-http://localhost:8080/}"
TES_VERSIONS="${TES_VERSIONS:-1.0.0 1.1.0}"
POLL_TIMEOUT="${POLL_TIMEOUT:-600}"
RUN_TIMEOUT="${RUN_TIMEOUT:-1500}"
for var in POLL_TIMEOUT RUN_TIMEOUT; do
    [[ "${!var}" =~ ^[0-9]+$ ]] || { echo "$var must be a whole number of seconds" >&2; exit 2; }
done
WORK_DIR="${WORK_DIR:-$TESTS_DIR/../.conformance}"
mkdir -p "$WORK_DIR"
WORK_DIR="$(cd "$WORK_DIR" && pwd)"

# The runner builds request URLs as <server>v1/<endpoint>, so it needs the trailing slash.
[[ "$TES_URL" == */ ]] || TES_URL="$TES_URL/"

RUNNER_DIR="$WORK_DIR/openapi-test-runner"
VENV_DIR="$WORK_DIR/venv"
REPORT_DIR="$WORK_DIR/report"

rm -rf "$REPORT_DIR"
mkdir -p "$REPORT_DIR"
# CI reads this to tell test failures (1) from a broken run (2).
trap 'echo $? > "$REPORT_DIR/exit-code"' EXIT

if [[ "$(git -C "$RUNNER_DIR" rev-parse HEAD 2>/dev/null)" != "$RUNNER_SHA" ]]; then
    rm -rf "$RUNNER_DIR"
    git init -q "$RUNNER_DIR"
    git -C "$RUNNER_DIR" fetch -q --depth 1 "$RUNNER_REPO" "$RUNNER_SHA"
    git -C "$RUNNER_DIR" -c advice.detachedHead=false checkout -q FETCH_HEAD
fi
# Only the polling timeout differs from the pinned suite; reset first so reruns stay idempotent.
git -C "$RUNNER_DIR" checkout -q -- tests templates
sed -i -E "s/^( *(timeout|polling_timeout_value): *)3600\b/\1$POLL_TIMEOUT/" \
    "$RUNNER_DIR"/tests/*.yml "$RUNNER_DIR"/templates/*.yml
if ! grep -rqE "(timeout|polling_timeout_value): *$POLL_TIMEOUT\b" "$RUNNER_DIR"/tests "$RUNNER_DIR"/templates; then
    echo "polling timeout patch no longer applies to the pinned runner" >&2
    exit 2
fi

if [[ ! -f "$VENV_DIR/.installed-$RUNNER_SHA" ]]; then
    rm -rf "$VENV_DIR"
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install -q -r "$RUNNER_DIR/requirements.txt"
    touch "$VENV_DIR/.installed-$RUNNER_SHA"
fi

curl -fsS "${TES_URL}v1/service-info" > "$REPORT_DIR/service-info.json"

status=0
for version in $TES_VERSIONS; do
    out="$REPORT_DIR/$version"
    mkdir -p "$out"
    echo "== TES $version against $TES_URL"
    # The runner resolves tests/, templates/ and docs/ relative to its own checkout.
    # It writes report.json only at the very end, so a timeout leaves just run.log.
    if ! (cd "$RUNNER_DIR" && timeout "$RUN_TIMEOUT" "$VENV_DIR/bin/python" -m compliance_suite.cli report \
            --server "$TES_URL" --version "$version" --output_path "$out") >"$out/run.log" 2>&1; then
        echo "runner crashed or timed out (${RUN_TIMEOUT}s) for TES $version; see $out/run.log" >&2
        tail -n 50 "$out/run.log" >&2
        printf '### TES %s\n\nRunner crashed or timed out after %ss; see run.log.\n\n' "$version" "$RUN_TIMEOUT" \
            > "$out/summary.md"
        status=2
        continue
    fi
    rc=0
    "$VENV_DIR/bin/python" -I "$TESTS_DIR/conformance_summary.py" \
        "$out/report.json" "$out/run.log" "$version" | tee "$out/summary.md" || rc=$?
    if (( rc > status )); then status=$rc; fi
done
exit "$status"
