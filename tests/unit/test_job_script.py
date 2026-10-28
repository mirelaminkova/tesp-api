"""Unit tests for the Pulsar job script (issues #18, #30).

Pulsar stores the job's stderr as ``ExecutorLog.stderr``, so the script must not trace itself (``set -x``): the trace
would put executor env values, presigned URLs and host paths into the task's logs. The tests run the script with the
host's ``sh``, with ``echo``/``false`` standing in for the container commands.
"""
import shlex
import subprocess

from tesp_api.utils.job_script import job_script

SECRET = "s3cr3t"
SIGNED_URL = "https://s3.example.org/b/k?X-Amz-Credential=AKIA&X-Amz-Signature=abc123"


def _run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True)


def test_no_commands_is_no_script():
    assert job_script("", "  ", None) is None


def test_script_is_not_traced():
    script = job_script(
        f"echo stage-in >/dev/null {shlex.quote(SIGNED_URL)}",
        f"SECRET={SECRET} sh -c 'echo hi'",
        f"echo stage-out >/dev/null {shlex.quote(SIGNED_URL)}",
    )
    result = _run(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "hi\n"
    assert result.stderr == ""


def test_failing_stage_in_ends_the_job_with_its_exit_code():
    result = _run(job_script("echo stage-in && sh -c 'exit 4' && echo not-reached", "echo executor", "echo stage-out"))
    assert result.returncode == 4
    assert result.stdout == "stage-in\n"


# Issue #30: outputs are uploaded also when an executor fails; the task still ends in EXECUTOR_ERROR.

def test_stage_out_runs_after_a_failed_executor():
    result = _run(job_script("echo stage-in", "sh -c 'exit 3'", "echo stage-out"))
    assert result.returncode == 3
    assert result.stdout == "stage-in\nstage-out\n"


def test_executors_after_a_failed_one_do_not_run():
    result = _run(job_script("", "echo 1", "mkdir -p /dev/null/x 2>/dev/null && echo not-reached", "echo 3",
                             "echo stage-out"))
    assert result.returncode == 1
    assert result.stdout == "1\nstage-out\n"


def test_failed_executor_code_wins_over_failed_stage_out():
    result = _run(job_script("", "sh -c 'exit 3'", "sh -c 'exit 5'"))
    assert result.returncode == 3


def test_failed_stage_out_after_successful_executors_fails_the_job():
    result = _run(job_script("echo stage-in", "echo executor", "echo stage-out && sh -c 'exit 5' && echo not-reached"))
    assert result.returncode == 5
    assert result.stdout == "stage-in\nexecutor\nstage-out\n"


def test_successful_job_exits_zero():
    result = _run(job_script("", "echo executor", ""))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "executor\n"


def test_stage_out_alone():
    # A two-command call is stage-in and stage-out, with no executors in between.
    result = _run(job_script("echo stage-in", "echo stage-out"))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "stage-in\nstage-out\n"


def test_stages_run_in_order():
    result = _run(job_script("echo 1", "echo 2", "echo 3"))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "1\n2\n3\n"
