"""Unit tests for the executor ``ignore_error`` flag (TES 1.1, issue #23).

"Default behavior of running an array of executors is that execution stops on the first error. If ``ignore_error`` is
``True``, then the runner will record error exit codes, but will continue on to the next tesExecutor." The job scripts
run with the host's shells; the task tests run the job script ``handle_run_task`` submits, with a ``docker`` stub.
"""
import subprocess
from pathlib import Path

import pytest

from tesp_api.repository.model.task import TesTask, TesTaskExecutor, TesTaskState
from tesp_api.utils.job_script import executor_file, job_script, parse_executor_status
from tests.unit.run_task_stub import ScriptPulsar, run_task


@pytest.fixture
def in_job_working_directory(tmp_path, monkeypatch):
    # Only for the job script tests: tesp-api's settings are read relative to the working directory.
    monkeypatch.chdir(tmp_path)


def _run(script: str, shell: str = "bash") -> str:
    """Runs the script as Pulsar does and returns the exit code it records."""
    subprocess.run([shell, "-c", f"{script}; return_code=$?; echo $return_code > return_code;"],
                   capture_output=True, text=True, timeout=30)
    return Path("return_code").read_text().strip()


def _exit_codes(count: int):
    return [parse_executor_status(Path(executor_file(i, "status")).read_bytes())[2]
            if Path(executor_file(i, "status")).exists() else None for i in range(count)]


@pytest.mark.parametrize("shell", ["bash", "sh"])
def test_ignored_error_continues_with_the_next_executor(shell, in_job_working_directory):
    assert _run(job_script("", "sh -c 'exit 3'", "echo second", "echo stage-out > stage-out", ignore_errors=[True]),
                shell) == "0"
    assert _exit_codes(2) == [3, 0]
    assert Path(executor_file(1, "stdout")).read_text() == "second\n"
    assert Path("stage-out").exists()


def test_error_stops_by_default(in_job_working_directory):
    assert _run(job_script("", "sh -c 'exit 3'", "echo second", "", ignore_errors=[False])) == "3"
    assert _exit_codes(2) == [3, None]


def test_later_error_still_fails_the_job(in_job_working_directory):
    script = job_script("", "sh -c 'exit 3'", "sh -c 'exit 5'", "echo third", "echo stage-out > stage-out",
                        ignore_errors=[True, False, False])
    assert _run(script) == "5"
    assert _exit_codes(3) == [3, 5, None]
    assert not Path("stage-out").exists()


def test_flags_follow_executor_positions(in_job_working_directory):
    # The second executor has nothing to run; the flag of the third still applies to the third.
    script = job_script("", "echo first", "", "sh -c 'exit 4'", "echo fourth", "", ignore_errors=[False, False, True])
    assert _run(script) == "0"
    assert _exit_codes(4) == [0, None, 4, 0]


def test_ignore_error_is_part_of_the_task_model():
    task = TesTask(executors=[{"image": "alpine", "command": ["ERROR"], "ignore_error": True},
                              {"image": "alpine", "command": ["echo", "hello"]}])
    assert [executor.ignore_error for executor in task.executors] == [True, None]
    assert task.dict()["executors"][0]["ignore_error"] is True


@pytest.mark.parametrize("ignore_error, state, logs", [
    # The two halves of the conformance test tests/create_task_ignore_error.yml (ga4gh/openapi-test-runner)
    (False, TesTaskState.EXECUTOR_ERROR, [("", 127)]),
    (True, TesTaskState.COMPLETE, [("", 127), ("hello\n", 0)]),
])
def test_task_with_failing_first_executor(monkeypatch, tmp_path, ignore_error, state, logs):
    workdir = tmp_path / "working"
    workdir.mkdir()
    executors = [TesTaskExecutor(image="alpine", command=["tesp-no-such-command"], ignore_error=ignore_error),
                 TesTaskExecutor(image="alpine", command=["echo", "hello"])]
    task, _ = run_task(monkeypatch, None, executors, pulsar=ScriptPulsar(workdir))
    assert task.state == state
    assert [(log.stdout, log.exit_code) for log in task.executor_logs] == logs
