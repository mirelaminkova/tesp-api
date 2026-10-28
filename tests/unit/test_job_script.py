"""Unit tests for the Pulsar job script (issues #18 and #20).

Pulsar stores the job's stderr as ``ExecutorLog.stderr``, so the script must not trace itself (``set -x``): the trace
would put executor env values, presigned URLs and host paths into the task's logs. Pulsar also decodes the job's
stdout and stderr as strict UTF-8, so binary output must not reach it. The tests run the script with the host's
``sh`` in a temporary directory standing in for Pulsar's job working directory, with ``echo``/``false``/``head``
standing in for the container commands.
"""
import shlex
import subprocess

import pytest

from tesp_api.utils.container import STDIO_LOG_TAIL_BYTES
from tesp_api.utils.job_script import job_script

SECRET = "s3cr3t"
SIGNED_URL = "https://s3.example.org/b/k?X-Amz-Credential=AKIA&X-Amz-Signature=abc123"


def _run(script: str, cwd) -> subprocess.CompletedProcess:
    # Bytes, decoded strictly the way Pulsar decodes job_stdout/job_stderr
    result = subprocess.run(["sh", "-c", script], capture_output=True, cwd=cwd, timeout=30)
    result.stdout = result.stdout.decode("utf-8")
    result.stderr = result.stderr.decode("utf-8")
    return result


def test_no_commands_is_no_script():
    assert job_script("", "  ", None) is None


def test_script_is_not_traced(tmp_path):
    script = job_script(
        f"echo stage-in >/dev/null {shlex.quote(SIGNED_URL)}",
        f"SECRET={SECRET} sh -c 'echo hi'",
        f"echo stage-out >/dev/null {shlex.quote(SIGNED_URL)}",
    )
    result = _run(script, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "hi\n"
    assert result.stderr == ""


def test_failing_stage_ends_the_job_with_its_exit_code(tmp_path):
    result = _run(job_script("echo stage-in", "sh -c 'exit 3'", "echo stage-out"), tmp_path)
    assert result.returncode == 3
    assert result.stdout == "stage-in\n"


def test_stages_run_in_order(tmp_path):
    result = _run(job_script("echo 1", "echo 2", "echo 3"), tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "1\n2\n3\n"


@pytest.mark.parametrize("command, returncode", [("head -c 100000 /dev/urandom", 0),
                                                 ("head -c 100000 /dev/urandom; exit 4", 4)])
def test_binary_output_reaches_pulsar_as_text(tmp_path, command, returncode):
    # Issue #20: an executor writing binary data to its own streams, not redirected to a file.
    script = job_script(f"sh -c {shlex.quote(command)}", f"sh -c {shlex.quote(command + ' >&2')}")
    result = _run(script, tmp_path)  # would raise UnicodeDecodeError
    assert result.returncode == returncode
    assert result.stdout.isascii()
    assert result.stderr.isascii()


def test_text_survives_and_stderr_stays_apart(tmp_path):
    script = job_script("echo out", "sh -c 'printf \"err\\tline\\r\\n\" >&2'", "printf '\\377bin\\n'")
    result = _run(script, tmp_path)
    assert result.returncode == 0
    assert result.stdout == "out\nbin\n"
    assert result.stderr == "err\tline\r\n"


def test_only_the_tail_is_passed_on(tmp_path):
    size = STDIO_LOG_TAIL_BYTES * 3
    result = _run(job_script(f"head -c {size} /dev/zero | tr '\\0' x", "echo end"), tmp_path)
    assert result.returncode == 0
    assert len(result.stdout) == STDIO_LOG_TAIL_BYTES
    assert result.stdout.endswith("xend\n")


def test_capture_files_are_removed(tmp_path):
    _run(job_script("echo hi", "false"), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_stdin_still_reaches_the_stages(tmp_path):
    result = subprocess.run(["sh", "-c", job_script("cat")], input=b"piped\n", capture_output=True, cwd=tmp_path)
    assert result.stdout == b"piped\n"
