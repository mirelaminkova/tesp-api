#!/usr/bin/env bash
# Run the GA4GH TES conformance suite (ga4gh/openapi-test-runner) against a
# running TESP-API and write one JSON report per TES version.
#
# Usage: bash tests/conformance.sh
#
# Environment (all optional):
#   TES_URL        server root, without the /v1 suffix (default http://localhost:8080/)
#   TES_VERSIONS   space-separated TES versions to test (default "1.0.0 1.1.0")
#   WORK_DIR       where the runner, its virtualenv and the reports go (default .conformance)
#   POLL_TIMEOUT   seconds a test waits for a task to reach a terminal state (default 600;
#                  the suite ships 3600, which turns one stuck task into an hour-long run)
#
# Exits 0 when every test passes, 1 when any test fails, 2 when the run itself breaks.

set -euo pipefail

# Pinned: the runner has no tags or releases. Bump deliberately.
RUNNER_REPO="https://github.com/ga4gh/openapi-test-runner.git"
RUNNER_SHA="bc3eb911e877521409348e0225852d072d4de196"

TES_URL="${TES_URL:-http://localhost:8080/}"
TES_VERSIONS="${TES_VERSIONS:-1.0.0 1.1.0}"
POLL_TIMEOUT="${POLL_TIMEOUT:-600}"
WORK_DIR="$(mkdir -p "${WORK_DIR:-.conformance}" && cd "${WORK_DIR:-.conformance}" && pwd)"

# The runner builds request URLs as <server>v1/<endpoint>, so it needs the trailing slash.
[[ "$TES_URL" == */ ]] || TES_URL="$TES_URL/"

RUNNER_DIR="$WORK_DIR/openapi-test-runner"
VENV_DIR="$WORK_DIR/venv"
REPORT_DIR="$WORK_DIR/report"

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

if [[ ! -f "$VENV_DIR/.installed-$RUNNER_SHA" ]]; then
    rm -rf "$VENV_DIR"
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install -q -r "$RUNNER_DIR/requirements.txt"
    touch "$VENV_DIR/.installed-$RUNNER_SHA"
fi

rm -rf "$REPORT_DIR"
status=0
for version in $TES_VERSIONS; do
    out="$REPORT_DIR/$version"
    mkdir -p "$out"
    echo "== TES $version against $TES_URL"
    # The runner resolves tests/, templates/ and docs/ relative to its own checkout.
    if ! (cd "$RUNNER_DIR" && "$VENV_DIR/bin/python" -m compliance_suite.cli report \
            --server "$TES_URL" --version "$version" --output_path "$out") >"$out/run.log" 2>&1; then
        echo "runner crashed for TES $version; see $out/run.log" >&2
        tail -n 50 "$out/run.log" >&2
        exit 2
    fi
    # Prints a per-test pass/fail table; exits 1 when anything failed, 2 if the report is missing.
    rc=0
    "$VENV_DIR/bin/python" -I - "$out/report.json" "$out/run.log" "$version" <<'EOF' | tee "$out/summary.md" || rc=$?
import json
import re
import sys

path, log_path, version = sys.argv[1:4]
try:
    with open(path) as fh:
        report = json.load(fh)
    with open(log_path) as fh:
        # A test that fails YAML validation has no FAIL case in the report; the log still names it.
        log_failed = set(re.findall(r"Compliance Test-\d+ for (\S+) failed\.", fh.read()))
except (OSError, ValueError) as err:
    print(f"no usable report at {path}: {err}", file=sys.stderr)
    sys.exit(2)

failed = 0
print(f"### TES {version}\n")
print("| test file | status | failing cases |")
print("|---|---|---|")
for phase in report.get("phases", []):
    name = phase.get("phase_name", "?").rsplit("/", 1)[-1]
    cases = [c for t in phase.get("tests", []) for c in t.get("cases", [])]
    bad = [c for c in cases if c.get("status") == "FAIL"]
    status = phase.get("status")
    if status == "FAIL" or bad or phase.get("phase_name") in log_failed:
        failed += 1
        status = "FAIL"
    reasons = "<br>".join(
        f"{c.get('case_name')}: {(c.get('message') or '').splitlines()[0][:200] if c.get('message') else ''}"
        for c in bad
    ).replace("|", "\\|")
    print(f"| {name} | {status} | {reasons} |")
summary = report.get("summary", {})
print(f"\nphases: {len(report.get('phases', []))}, failing: {failed}; cases: {summary}\n")
sys.exit(1 if failed else 0)
EOF
    if (( rc > status )); then status=$rc; fi
done
exit "$status"
