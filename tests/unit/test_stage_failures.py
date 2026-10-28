"""Unit tests for failures outside the executors (issue #66).

A failing stage-in or stage-out, and a job that Pulsar or the batch system never started, end the task in
SYSTEM_ERROR with the reason in ``TaskLog.system_logs``; executor failures stay EXECUTOR_ERROR. The job scripts run
with the host's shells in a temporary directory standing in for Pulsar's job working directory.
"""
import os
import shlex
import subprocess

import pytest

from tesp_api.repository.model.task import TesTaskExecutor, TesTaskState
from tesp_api.utils.job_script import JOB_STARTED_FILE, STAGE_FAILED_FILES, failed_stage, job_script, \
    stage_failure_log
from tests.unit.run_task_stub import run_task

CURL_404 = "curl: (22) The requested URL returned error: 404"
PROGRESS = ("  % Total    % Received % Xferd  Average Speed   Time\r"
            "  0     0    0     0    0     0      0      0 --:--:--\r")


def _run(script: str, cwd) -> subprocess.CompletedProcess:
    # As Pulsar runs it: in the job working directory, followed by its return-code capture, with bash
    command_line = f"{script}; return_code=$?; echo $return_code > return_code;"
    return subprocess.run(["bash", "-c", command_line], capture_output=True, text=True, cwd=cwd, timeout=30)


def _status(cwd, returncode=None, pulsar_status="complete", stderr="") -> dict:
    """A completed Pulsar job status for the job that ran in ``cwd``."""
    contents = sorted(name for name in os.listdir(cwd) if name != "return_code")
    if returncode is None:
        returncode = int((cwd / "return_code").read_text())
    return {"job_id": "abc", "complete": "true", "status": pulsar_status, "returncode": returncode,
            "job_stdout": "", "job_stderr": stderr, "working_directory_contents": contents}


# --- The job script ---------------------------------------------------------------------------------------------------

def test_failing_stage_in_skips_the_rest_and_is_marked(tmp_path):
    result = _run(job_script(f"echo {shlex.quote(CURL_404)} >&2; exit 22", "echo executor", "echo stage-out"),
                  tmp_path)
    assert result.stdout == ""
    assert (tmp_path / "return_code").read_text() == "22\n"
    assert failed_stage(_status(tmp_path)) == "stage-in"


def test_failing_stage_out_is_marked(tmp_path):
    result = _run(job_script("echo stage-in", "echo executor", "echo stage-out; exit 7"), tmp_path)
    assert result.stdout == "stage-in\nexecutor\nstage-out\n"
    assert (tmp_path / "return_code").read_text() == "7\n"
    assert failed_stage(_status(tmp_path)) == "stage-out"


def test_failing_executor_is_not_a_stage_failure(tmp_path):
    result = _run(job_script("echo stage-in", "sh -c 'exit 3'", "echo executor-2", "echo stage-out"), tmp_path)
    assert result.stdout == "stage-in\n"
    assert (tmp_path / "return_code").read_text() == "3\n"
    assert failed_stage(_status(tmp_path)) is None


def test_failing_part_of_a_stage_list_fails_the_stage(tmp_path):
    # Stage commands are lists such as "mkdir -p ... && docker run ..."; a failure left of the && counts too.
    _run(job_script("false && echo not-reached", "echo executor", ""), tmp_path)
    assert (tmp_path / "return_code").read_text() == "1\n"
    assert failed_stage(_status(tmp_path)) == "stage-in"


def test_successful_job_leaves_only_the_start_marker(tmp_path):
    result = _run(job_script("echo 1", "echo 2", "echo 3", "echo 4"), tmp_path)
    assert result.stdout == "1\n2\n3\n4\n"
    assert (tmp_path / "return_code").read_text() == "0\n"
    assert sorted(os.listdir(tmp_path)) == sorted([JOB_STARTED_FILE, "return_code"])


def test_stage_in_and_out_only(tmp_path):
    # A two-command call is stage-in and stage-out, with no executors in between.
    result = _run(job_script("echo stage-in", "echo stage-out; exit 4"), tmp_path)
    assert result.stdout == "stage-in\nstage-out\n"
    assert failed_stage(_status(tmp_path)) == "stage-out"


# --- Reading the Pulsar status ----------------------------------------------------------------------------------------

def test_job_that_never_started_is_a_submission_failure():
    # Pulsar's preprocessing failure (e.g. SLURM rejects the memory request): status failed, return code 1, the job
    # script never ran.
    status = {"job_id": "abc", "complete": "true", "status": "failed", "returncode": 1, "job_stdout": "",
              "job_stderr": "", "working_directory_contents": []}
    assert failed_stage(status) == "submission"


def test_failed_job_that_could_not_be_read_is_a_submission_failure():
    # Pulsar's answer when the job failed before launch and its directory cannot be read
    assert failed_stage({"job_id": "abc", "complete": "true", "status": "failed", "returncode": None}) == "submission"


def test_failed_job_that_started_is_not_a_submission_failure():
    status = {"status": "failed", "returncode": 137, "working_directory_contents": [JOB_STARTED_FILE]}
    assert failed_stage(status) is None


def test_older_pulsar_without_directory_listing_keeps_executor_error():
    assert failed_stage({"status": "complete", "returncode": 3, "job_stdout": "", "job_stderr": ""}) is None


@pytest.mark.parametrize("stage", list(STAGE_FAILED_FILES))
def test_marker_in_a_subdirectory_does_not_count(stage):
    status = {"status": "complete", "returncode": 1,
              "working_directory_contents": [JOB_STARTED_FILE, f"sub/{STAGE_FAILED_FILES[stage]}"]}
    assert failed_stage(status) is None


def test_system_log_quotes_the_tail_of_stderr_without_the_progress_meter():
    log = stage_failure_log("stage-in", 22, f"INFO:    Using cached SIF image\n{PROGRESS}{CURL_404}\n")
    assert log.startswith("stage-in failed (exit code 22): downloading the inputs failed\n")
    assert log.endswith(CURL_404)
    assert "\r" not in log


def test_system_log_is_bounded():
    log = stage_failure_log("stage-out", 1, "".join(f"line {i} " + "x" * 300 + "\n" for i in range(100)))
    assert len(log) < 4096 + 200
    assert "line 99" in log and "line 70" not in log


# --- The task's final state -------------------------------------------------------------------------------------------

EXECUTOR = TesTaskExecutor(image="ubuntu", command=["echo", "hi"])
INPUT = [{"container_path": "/data/in.txt", "pulsar_path": "/pulsar/abc/inputs/input_file_0",
          "url": "http://example.org/does-not-exist.txt", "type": "FILE"}]


def test_task_with_failed_stage_in_ends_in_system_error(monkeypatch):
    status = {"status": "complete", "returncode": 22, "job_stdout": "", "job_stderr": f"{PROGRESS}{CURL_404}\n",
              "working_directory_contents": [JOB_STARTED_FILE, STAGE_FAILED_FILES["stage-in"]]}
    task, pulsar = run_task(monkeypatch, status, [EXECUTOR], input_confs=INPUT)
    assert task.state == TesTaskState.SYSTEM_ERROR
    assert task.executor_logs == []  # the executor never ran
    [system_log] = task.system_logs
    assert "stage-in failed (exit code 22)" in system_log and CURL_404 in system_log
    assert pulsar.erased


def test_task_with_failed_stage_out_ends_in_system_error_with_executor_log(monkeypatch):
    status = {"status": "complete", "returncode": 6, "job_stdout": "hi\n", "job_stderr": "curl: (6) no host\n",
              "working_directory_contents": [JOB_STARTED_FILE, STAGE_FAILED_FILES["stage-out"]]}
    output = [{"container_path": "/data/out.txt", "url": "http://example.org/up", "volume_name": "v", "type": "FILE"}]
    task, _ = run_task(monkeypatch, status, [EXECUTOR], output_confs=output)
    assert task.state == TesTaskState.SYSTEM_ERROR
    assert [log["exit_code"] for log in task.executor_logs] == [0]
    assert "stage-out failed (exit code 6)" in task.system_logs[0]


def test_task_the_batch_system_refused_ends_in_system_error(monkeypatch):
    status = {"status": "failed", "returncode": 1, "job_stdout": "", "job_stderr": "",
              "working_directory_contents": []}
    task, _ = run_task(monkeypatch, status, [EXECUTOR])
    assert task.state == TesTaskState.SYSTEM_ERROR
    assert task.executor_logs == []
    assert "batch system" in task.system_logs[0]


def test_failing_executor_still_ends_in_executor_error(monkeypatch):
    status = {"status": "complete", "returncode": 3, "job_stdout": "before-fail\n", "job_stderr": "",
              "working_directory_contents": [JOB_STARTED_FILE]}
    task, _ = run_task(monkeypatch, status, [EXECUTOR])
    assert task.state == TesTaskState.EXECUTOR_ERROR
    assert task.executor_logs == [{"stdout": "before-fail\n", "stderr": "", "exit_code": 3}]
    assert task.system_logs == []


def test_successful_task_completes(monkeypatch):
    status = {"status": "complete", "returncode": 0, "job_stdout": "hi\n", "job_stderr": "",
              "working_directory_contents": [JOB_STARTED_FILE]}
    task, pulsar = run_task(monkeypatch, status, [EXECUTOR])
    assert task.state == TesTaskState.COMPLETE
    assert pulsar.script.startswith(f"( : > {JOB_STARTED_FILE}; ")
