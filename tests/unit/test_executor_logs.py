"""Unit tests for per-executor logs (issue #67).

Each executor's stdout, stderr and exit code go to their own files in the job's working directory; tesp-api reads them
from Pulsar and stores one ExecutorLog per executor that ran, without the stage-in and stage-out output. The job
scripts run with the host's shells in a temporary directory standing in for Pulsar's job working directory.
"""
import asyncio
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from aiohttp import ClientResponseError, RequestInfo
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from tesp_api.repository.model.task import TesTaskExecutor, TesTaskState
from tesp_api.service.pulsar_operations import PulsarLayerConnectionError, PulsarRestOperations
from tesp_api.utils.container import ContainerCommandBuilder
from tesp_api.utils.job_script import EXECUTOR_LOG_TAIL_BYTES, executor_file, job_script, parse_executor_status, \
    stream_text, tesp_api_notes
from tests.unit.run_task_stub import ScriptPulsar, run_task

PROGRESS = "  % Total    % Received % Xferd\r100 15543  100 15543\r"


@pytest.fixture
def in_job_working_directory(tmp_path, monkeypatch):
    # Only for the job script tests: tesp-api's settings are read relative to the working directory.
    monkeypatch.chdir(tmp_path)


def _run(script: str, shell: str = "bash") -> subprocess.CompletedProcess:
    return subprocess.run([shell, "-c", f"{script}; return_code=$?; echo $return_code > return_code;"],
                          capture_output=True, text=True, timeout=30)


def _file(index: int, kind: str) -> str:
    return Path(executor_file(index, kind)).read_text()


# --- The job script ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("shell", ["bash", "sh"])
def test_each_executor_gets_its_own_streams_and_exit_code(shell, in_job_working_directory):
    result = _run(job_script("echo stage-in-note >&2",
                             "sh -c 'echo step1; echo warn1 >&2'",
                             "sh -c 'echo step2; exit 3'",
                             "echo not-reached",
                             "echo stage-out"), shell)
    # The job's own streams keep only the stage output; the job ends with the failed executor's code.
    assert result.stdout == ""
    assert result.stderr == "stage-in-note\n"
    assert Path("return_code").read_text() == "3\n"
    assert (_file(0, "stdout"), _file(0, "stderr"), _file(1, "stdout"), _file(1, "stderr")) == \
           ("step1\n", "warn1\n", "step2\n", "")
    assert parse_executor_status(Path(executor_file(0, "status")).read_bytes())[2] == 0
    assert parse_executor_status(Path(executor_file(1, "status")).read_bytes())[2] == 3
    assert not Path(executor_file(2, "status")).exists()  # did not run


def test_status_holds_start_and_end_time(in_job_working_directory):
    before = int(datetime.now(timezone.utc).timestamp())
    _run(job_script("", "sleep 1", ""))
    start, end, exit_code = parse_executor_status(Path(executor_file(0, "status")).read_bytes())
    assert before <= start.timestamp() < end.timestamp() <= before + 10
    assert start.tzinfo is not None and exit_code == 0


def test_only_the_tail_of_a_stream_is_kept(in_job_working_directory):
    size = EXECUTOR_LOG_TAIL_BYTES * 3
    _run(job_script("", f"sh -c 'head -c {size} /dev/zero | tr \"\\0\" x; echo end'", ""))
    stdout = _file(0, "stdout")
    assert len(stdout) == EXECUTOR_LOG_TAIL_BYTES
    assert stdout.endswith("xend\n")
    assert sorted(p.name for p in Path().iterdir() if p.name.endswith(".tail")) == []


def test_executor_numbers_are_task_positions(in_job_working_directory):
    # An executor with nothing to run keeps the numbering of the ones after it.
    _run(job_script("", "", "echo second", ""))
    assert _file(1, "stdout") == "second\n"


def test_binary_output_is_stored_as_text():
    assert stream_text(b"ok \xff\xfe end") == "ok \ufffd\ufffd end"
    assert stream_text(None) == ""


def test_notes_of_the_stage_scripts():
    stderr = f"{PROGRESS}\ntesp-api: output /data/out.txt not found, skipped\nother\n"
    assert tesp_api_notes(stderr) == ["tesp-api: output /data/out.txt not found, skipped"]


# --- Pulsar -----------------------------------------------------------------------------------------------------------

class _Pulsar(PulsarRestOperations):
    def __init__(self, answer):
        super().__init__(None, "http://pulsar", None)
        self.answer, self.requests = answer, []

    async def _pulsar_request(self, path, method, response_type, params=None, data=None, raise_for_status=False):
        self.requests.append((path, method, params, raise_for_status))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _http_error(status: int) -> PulsarLayerConnectionError:
    url = URL("http://pulsar/jobs/abc/files")
    request_info = RequestInfo(url=url, method="GET", headers=CIMultiDictProxy(CIMultiDict()), real_url=url)
    return PulsarLayerConnectionError(ClientResponseError(request_info, (), status=status, message="err"))


def test_job_files_are_read_from_the_working_directory():
    pulsar = _Pulsar(b"content")
    assert asyncio.run(pulsar.read_job_file("abc", "tesp-executor-0.stdout")) == b"content"
    assert pulsar.requests == [("/jobs/abc/files", "GET", {"name": "tesp-executor-0.stdout",
                                                           "type": "output_workdir"}, True)]


def test_missing_job_file_is_none():
    assert asyncio.run(_Pulsar(_http_error(404)).read_job_file("abc", "x")) is None


def test_other_job_file_errors_are_raised():
    with pytest.raises(PulsarLayerConnectionError):
        asyncio.run(_Pulsar(_http_error(500)).read_job_file("abc", "x"))


def test_apptainer_runs_quietly():
    command = ContainerCommandBuilder("singularity").with_image("ubuntu").with_command(["true"]).get_run_command()
    assert command.startswith("singularity --quiet exec --no-eval ")


# --- The task's logs --------------------------------------------------------------------------------------------------

def test_task_gets_one_log_per_executor(monkeypatch, tmp_path):
    workdir = tmp_path / "working"
    workdir.mkdir()
    executors = [TesTaskExecutor(image="ubuntu", command=["sh", "-c", "echo step1 > /dev/null; echo step1"]),
                 TesTaskExecutor(image="ubuntu", command=["sh", "-c", "echo step2; echo oops >&2; exit 5"]),
                 TesTaskExecutor(image="ubuntu", command=["echo", "not-reached"])]
    output = [{"container_path": str(tmp_path / "missing.txt"), "url": "http://example.org/up", "volume_name": "v",
               "type": "FILE"}]
    task, pulsar = run_task(monkeypatch, None, executors, output_confs=output, pulsar=ScriptPulsar(workdir))

    assert task.state == TesTaskState.EXECUTOR_ERROR
    assert [(log.stdout, log.stderr, log.exit_code) for log in task.executor_logs] == \
           [("step1\n", "", 0), ("step2\n", "oops\n", 5)]
    assert all(log.start_time <= log.end_time for log in task.executor_logs)
    assert task.system_logs == []  # stage-out did not run after the failed executor


def test_stage_notes_go_to_system_logs(monkeypatch, tmp_path):
    workdir = tmp_path / "working"
    workdir.mkdir()
    output = [{"container_path": str(tmp_path / "missing.txt"), "url": "http://example.org/up", "volume_name": "v",
               "type": "FILE"}]
    task, _ = run_task(monkeypatch, None, [TesTaskExecutor(image="ubuntu", command=["echo", "hi"])],
                       output_confs=output, pulsar=ScriptPulsar(workdir))
    assert task.state == TesTaskState.COMPLETE
    assert [(log.stdout, log.stderr, log.exit_code) for log in task.executor_logs] == [("hi\n", "", 0)]
    assert task.system_logs == [f"tesp-api: output {tmp_path}/missing.txt not found, skipped"]


def test_older_pulsar_gets_one_log_for_the_job(monkeypatch):
    status = {"status": "complete", "returncode": 0, "job_stdout": "a\nb\n", "job_stderr": ""}
    executors = [TesTaskExecutor(image="ubuntu", command=["echo", "a"]),
                 TesTaskExecutor(image="ubuntu", command=["echo", "b"])]
    task, _ = run_task(monkeypatch, status, executors)
    assert task.state == TesTaskState.COMPLETE
    assert [(log.stdout, log.exit_code) for log in task.executor_logs] == [("a\nb\n", 0)]


def test_unreadable_executor_file_does_not_fail_the_task(monkeypatch):
    status = {"status": "complete", "returncode": 0, "job_stdout": "", "job_stderr": "",
              "working_directory_contents": ["tesp-job-started", executor_file(0, "status")]}
    files = {executor_file(0, "status"): b"garbage"}
    task, _ = run_task(monkeypatch, status, [TesTaskExecutor(image="ubuntu", command=["true"])], files=files)
    assert task.state == TesTaskState.COMPLETE
    assert task.executor_logs == []
