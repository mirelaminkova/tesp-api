"""Summarise one openapi-test-runner report as a Markdown table.

Usage: python conformance_summary.py REPORT_JSON RUN_LOG TES_VERSION

Exits 0 when every test file passed, 1 when any failed, 2 when the report is unusable.
"""

import json
import re
import sys

# The runner logs this line for every failed test file. A file that fails YAML
# validation has no FAIL case in report.json, so the log is the only signal.
LOG_FAILED = re.compile(r"Compliance Test-\d+ for (\S+) failed\.")


def main(report_path: str, log_path: str, version: str) -> int:
    try:
        with open(report_path) as fh:
            report = json.load(fh)
        with open(log_path) as fh:
            log_failed = set(LOG_FAILED.findall(fh.read()))
    except (OSError, ValueError) as err:
        print(f"no usable report at {report_path}: {err}", file=sys.stderr)
        return 2

    phases = report.get("phases", [])
    failed = 0
    print(f"### TES {version}\n")
    print("| test file | status | failing cases |")
    print("|---|---|---|")
    for phase in phases:
        name = phase.get("phase_name", "?")
        cases = [c for t in phase.get("tests", []) for c in t.get("cases", [])]
        bad = [c for c in cases if c.get("status") == "FAIL"]
        status = phase.get("status")
        if status == "FAIL" or bad or name in log_failed:
            failed += 1
            status = "FAIL"
        reasons = "<br>".join(
            f"{c.get('case_name')}: {(c.get('message') or '').partition(chr(10))[0][:200]}" for c in bad
        ) or ("see run.log" if status == "FAIL" else "")
        print(f"| {name.rsplit('/', 1)[-1]} | {status} | {reasons.replace('|', chr(92) + '|')} |")
    # A test file whose YAML fails validation never gets a phase; it only shows up in the log.
    for name in sorted(log_failed - {p.get("phase_name") for p in phases}):
        failed += 1
        print(f"| {name.rsplit('/', 1)[-1]} | FAIL | invalid test file, see run.log |")
    print(f"\ntest files: {len(phases)}, failing: {failed}; cases: {report.get('summary', {})}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
