"""Unit tests for the Pulsar job script (issues #18, #64).

Pulsar stores the job's stderr as ``ExecutorLog.stderr``, so the script must not trace itself (``set -x``): the trace
would put executor env values, presigned URLs and host paths into the task's logs. The tests run the script with the
host's ``sh``, with ``echo``/``false`` standing in for the container commands, in a temporary directory standing in
for Pulsar's job working directory (the script writes marker files there).
"""
import shlex
import subprocess
from pathlib import Path

import pytest

from tesp_api.utils.job_script import executor_file, job_script


@pytest.fixture(autouse=True)
def _in_job_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

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
    assert result.stdout == ""
    assert result.stderr == ""
    assert Path(executor_file(0, "stdout")).read_text() == "hi\n"
    assert Path(executor_file(0, "stderr")).read_text() == ""


def test_failing_executor_ends_the_job_with_its_exit_code_after_stage_out():
    # Stage-out runs after a failed executor (issue #30); the job still ends with the executor's code.
    result = _run(job_script("echo stage-in", "sh -c 'exit 3'", "echo not-reached >> order", "echo stage-out"))
    assert result.returncode == 3
    assert result.stdout == "stage-in\nstage-out\n"
    assert not Path("order").exists()


def test_failing_stage_in_ends_the_job_before_the_executors():
    result = _run(job_script("sh -c 'echo stage-in; exit 4'", "echo executor > ran", "echo stage-out"))
    assert result.returncode == 4
    assert result.stdout == "stage-in\n"
    assert not Path("ran").exists()


def test_stages_run_in_order():
    # Executors' output goes to their own files (issue #67); the stage output stays on the job's streams.
    result = _run(job_script("echo 1", "echo 2 >> order", "echo 3 >> order", "cat order"))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "1\n2\n3\n"


# Issue #64: Pulsar appends its return-code capture to the command line, in the same shell (bash, from its job
# script template, pulsar/managers/base/directory.py); the script must not end that shell before it runs.

def _run_as_pulsar(script: str, tmp_path) -> str:
    rc_path = tmp_path / "return_code"
    command_line = f"{script}; return_code=$?; echo $return_code > {shlex.quote(str(rc_path))};"
    subprocess.run(["bash", "-c", command_line], capture_output=True, text=True)
    return rc_path.read_text().strip() if rc_path.exists() else None


def test_pulsar_captures_exit_code_of_failing_last_stage(tmp_path):
    # One failing executor, no inputs or outputs: the executor is the last stage.
    assert _run_as_pulsar(job_script("", "sh -c 'echo before-fail; exit 3'", ""), tmp_path) == "3"


def test_pulsar_captures_exit_code_of_failing_earlier_stage(tmp_path):
    assert _run_as_pulsar(job_script("echo stage-in", "sh -c 'exit 5'", "echo stage-out"), tmp_path) == "5"


def test_pulsar_captures_exit_code_of_successful_job(tmp_path):
    assert _run_as_pulsar(job_script("echo stage-in", "echo executor", "echo stage-out"), tmp_path) == "0"


# Issue #30: stage-out runs after a failed executor. Under SLURM, Pulsar's queued_drmaa manager appends its return-code
# capture (pulsar/managers/base/directory.py: "; return_code=$?; echo $return_code > <dir>/return_code;") to the
# command line in the same shell, so the job script's own `exit` must stay inside its subshell.

@pytest.mark.parametrize("shell", ["bash", "sh"])
@pytest.mark.parametrize("executors, stage_out, return_code, uploaded, marker", [
    # A failed executor: outputs uploaded, its exit code kept, no stage marker (EXECUTOR_ERROR).
    (["sh -c 'echo partial > out; exit 3'", "echo not-reached > second"], "cp out uploaded", "3", True, False),
    # The executors succeeded and stage-out failed: its code and the stage-out marker (SYSTEM_ERROR).
    (["echo done > out"], "sh -c 'exit 7'", "7", False, True),
    # Both failed: the executor's code, no marker, a note on stderr.
    (["sh -c 'exit 3'"], "sh -c 'exit 7'", "3", False, False),
    # All good.
    (["echo done > out"], "cp out uploaded", "0", True, False),
])
def test_pulsar_queued_drmaa_captures_the_exit_code_after_stage_out(tmp_path, shell, executors, stage_out,
                                                                    return_code, uploaded, marker):
    from tesp_api.utils.job_script import STAGE_FAILED_FILES, STAGE_OUT_FAILED_NOTE
    rc_path = tmp_path / "return_code"
    command_line = f"{job_script('', *executors, stage_out)}; return_code=$?; echo $return_code > {rc_path};"
    result = subprocess.run([shell, "-c", command_line], capture_output=True, text=True, timeout=30)
    assert rc_path.read_text() == f"{return_code}\n"
    assert Path("uploaded").exists() is uploaded
    assert Path(STAGE_FAILED_FILES["stage-out"]).exists() is marker
    assert not Path("second").exists()
    assert (STAGE_OUT_FAILED_NOTE in result.stderr) is (return_code == "3" and not uploaded)


def test_stage_out_tries_every_output_before_it_fails(tmp_path):
    # After a failed executor an output can be left in a bad state (here a file where a DIRECTORY is declared); the
    # outputs after it must still be tried (issue #30).
    from tesp_api.utils.container import stage_out_script
    (tmp_path / "not-a-dir").write_text("x")
    script = stage_out_script([
        {"container_path": str(tmp_path / "not-a-dir"), "url": "http://127.0.0.1:9/up", "type": "DIRECTORY"},
        {"container_path": str(tmp_path / "missing"), "url": "http://127.0.0.1:9/up", "type": "FILE"}])
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert f"tesp-api: uploading output {tmp_path}/not-a-dir failed" in result.stderr
    assert f"tesp-api: output {tmp_path}/missing not found, skipped" in result.stderr


@pytest.mark.parametrize("shell", ["bash", "sh"])
def test_ignored_failure_then_stage_out(tmp_path, shell):
    # ignore_error: the job goes on and ends with 0 after stage-out; a failing stage-out is then a stage failure.
    from tesp_api.utils.job_script import STAGE_FAILED_FILES
    for stage_out, return_code, marker in [("echo up > uploaded", "0", False), ("sh -c 'exit 7'", "7", True)]:
        for name in ("uploaded", STAGE_FAILED_FILES["stage-out"]):
            Path(name).unlink(missing_ok=True)
        assert _run_as_pulsar_with(shell, job_script("", "sh -c 'exit 3'", stage_out, ignore_errors=[True]),
                                   tmp_path) == return_code
        assert Path(STAGE_FAILED_FILES["stage-out"]).exists() is marker


@pytest.mark.parametrize("shell", ["bash", "sh"])
def test_failing_executor_without_outputs(tmp_path, shell):
    assert _run_as_pulsar_with(shell, job_script("echo stage-in", "sh -c 'exit 3'", "echo not-reached > ran", ""),
                               tmp_path) == "3"
    assert not Path("ran").exists()


def _run_as_pulsar_with(shell: str, script: str, tmp_path) -> str:
    rc_path = tmp_path / "return_code"
    rc_path.unlink(missing_ok=True)
    subprocess.run([shell, "-c", f"{script}; return_code=$?; echo $return_code > {rc_path};"], capture_output=True,
                   timeout=30)
    return rc_path.read_text().strip()
