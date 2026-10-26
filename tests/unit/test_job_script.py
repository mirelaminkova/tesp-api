"""Unit tests for the Pulsar job script (issue #18).

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


def test_failing_stage_ends_the_job_with_its_exit_code():
    result = _run(job_script("echo stage-in", "sh -c 'exit 3'", "echo stage-out"))
    assert result.returncode == 3
    assert result.stdout == "stage-in\n"


def test_stages_run_in_order():
    result = _run(job_script("echo 1", "echo 2", "echo 3"))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "1\n2\n3\n"
