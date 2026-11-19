"""Runs ``handle_run_task`` against stubs: an in-memory task repository and a fake Pulsar that answers with a given
job status. The real task repository imports motor 2.5, which needs Python < 3.11 (asyncio.coroutine), and the real
Pulsar service opens an aiohttp session at import, so both are replaced in ``sys.modules`` for the import of
``tesp_api.service.event_actions``.
"""
import asyncio
import importlib
import os
import signal
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Dict, List, Optional

from pymonad.maybe import Just
from pymonad.promise import Promise

from tesp_api.repository.model.task import TesTaskExecutor, TesTaskExecutorLog, TesTaskState

# Modules that import the task repository or the Pulsar service, directly or through each other
_STUBBED_IMPORTERS = ('tesp_api.service.event_actions', 'tesp_api.service.error',
                      'tesp_api.repository.task_repository_utils', 'tesp_api.service.task_recovery')


def _resolved(value=None) -> Promise:
    return Promise(lambda resolve, reject: resolve(value))


class FakeTask:
    def __init__(self, executors: List[TesTaskExecutor]):
        self.executors = executors
        self.state = TesTaskState.INITIALIZING
        self.executor_logs: List[TesTaskExecutorLog] = []
        self.system_logs: List[str] = []


class FakeRepository:
    def __init__(self, task: FakeTask):
        self.task = task

    async def update_task_state(self, task_id, from_state, to_state):
        if from_state is None or self.task.state == from_state:
            self.task.state = to_state
        return Just(self.task)

    async def get_task(self, author, query):
        return Just(self.task)

    def update_raw_task(self, query, update):
        self.update_tasks(query, update)
        return _resolved(Just({'_id': query.get('_id')}))

    def update_tasks(self, query, update):
        # The lease and submission marks of tesp_api.service.task_recovery
        self.task.marks = {**getattr(self.task, 'marks', {}), **update.get('$set', {})}
        return _resolved(1)

    def update_task(self, query, update):
        self.task.state = update['$set']['state']
        if '$push' in update:
            self.task.system_logs.append(update['$push']['logs.$[].system_logs'])
        return _resolved(Just(self.task))


class FakePulsar:
    def __init__(self, status: dict, files: Dict[str, bytes]):
        self.status = status
        self.files = files
        self.script: Optional[str] = None
        self.erased = False

    async def read_job_file(self, job_id, name, tail_bytes=None):
        content = self.files.get(name)
        return content if content is None or tail_bytes is None else content[-tail_bytes:]

    async def run_job(self, task_id, script, submit_params=None):
        self.script = script

    async def job_status_complete(self, job_id, is_canceled, on_started=None):
        # As poll_until_complete: the complete status also says the job has started.
        if on_started is not None:
            await on_started()
        return self.status

    def erase_job(self, task_id):
        self.erased = True
        return _resolved()


# Runs a container command on the host: skips `run`, the --option=value flags and the image.
DOCKER_STUB = """#!/bin/sh
shift
while [ "${1#--}" != "$1" ]; do shift; done
shift
exec "$@"
"""


class ScriptPulsar(FakePulsar):
    """Runs the submitted job script with bash in ``workdir`` (standing in for the job's working directory), followed by
    Pulsar's return-code capture, with a ``docker`` stub that runs the container command on the host; answers the
    status and file requests from that directory."""

    def __init__(self, workdir: Path, kill_when: Optional[str] = None):
        """``kill_when``: a file name; once the job writes it, the whole job is killed with SIGKILL, as SLURM does at
        the job's time or memory limit (Pulsar's return-code capture then never runs)."""
        super().__init__({}, {})
        self.workdir = workdir
        self.kill_when = kill_when
        self.result: Optional[subprocess.CompletedProcess] = None

    async def run_job(self, task_id, script, submit_params=None):
        self.script = script
        bin_dir = self.workdir.parent / "bin"
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / "docker").write_text(DOCKER_STUB)
        (bin_dir / "docker").chmod(0o755)
        env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        args = ["bash", "-c", f"{script}; return_code=$?; echo $return_code > ../return_code;"]
        if self.kill_when is None:
            self.result = subprocess.run(args, cwd=self.workdir, env=env, capture_output=True, timeout=60)
            return
        process = subprocess.Popen(args, cwd=self.workdir, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        deadline = time.monotonic() + 30
        while not (self.workdir / self.kill_when).exists():
            assert process.poll() is None and time.monotonic() < deadline, "the job never wrote " + self.kill_when
            time.sleep(0.05)
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate(timeout=30)
        self.result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)

    async def job_status_complete(self, job_id, is_canceled, on_started=None):
        if on_started is not None:
            await on_started()
        return_code = self.workdir.parent / "return_code"
        return {"job_id": job_id, "complete": "true", "status": "complete" if return_code.exists() else "failed",
                # Pulsar's answer when the return_code file was never written
                "returncode": int(return_code.read_text()) if return_code.exists() else "__unknown__",
                "job_stdout": self.result.stdout.decode(), "job_stderr": self.result.stderr.decode(),
                "working_directory_contents": sorted(os.listdir(self.workdir))}

    async def read_job_file(self, job_id, name, tail_bytes=None):
        path = self.workdir / name
        content = path.read_bytes() if path.is_file() else None
        return content if content is None or tail_bytes is None else content[-tail_bytes:]


def import_event_actions(monkeypatch):
    repository = types.ModuleType('tesp_api.repository.task_repository')
    repository.task_repository = None  # set per run
    service = types.ModuleType('tesp_api.service.pulsar_service')
    service.pulsar_service = None
    monkeypatch.setitem(sys.modules, 'tesp_api.repository.task_repository', repository)
    monkeypatch.setitem(sys.modules, 'tesp_api.service.pulsar_service', service)
    for name in _STUBBED_IMPORTERS:
        # Undone after the test, so no copy bound to the stubs is left behind
        monkeypatch.setitem(sys.modules, name, None)
        del sys.modules[name]
    return importlib.import_module('tesp_api.service.event_actions')


def run_task(monkeypatch, status: Optional[dict], executors: List[TesTaskExecutor], input_confs: List[dict] = (),
             output_confs: List[dict] = (), files: Dict[str, bytes] = None, pulsar: FakePulsar = None):
    """Runs ``handle_run_task`` for one task. Pulsar answers with ``status`` and ``files`` (the job's working directory
    files), unless another ``pulsar`` is given. Returns the fake task and Pulsar."""
    event_actions = import_event_actions(monkeypatch)
    task = FakeTask(executors)
    repository = FakeRepository(task)
    monkeypatch.setattr(event_actions, 'task_repository', repository)
    monkeypatch.setattr(sys.modules['tesp_api.service.error'], 'task_repository', repository)
    monkeypatch.setattr(event_actions.task_recovery, 'task_repository', repository)

    async def append_task_executor_logs(task_id, author, state, end, executor_logs, system_logs=()):
        task.executor_logs.extend(executor_logs)
        task.system_logs.extend(system_logs)

    async def update_last_task_log_time(*args, **kwargs):
        pass

    monkeypatch.setattr(event_actions, 'append_task_executor_logs', append_task_executor_logs)
    monkeypatch.setattr(event_actions, 'update_last_task_log_time', update_last_task_log_time)

    pulsar = pulsar or FakePulsar(status, files or {})
    payload = {
        'task_id': 'abc', 'author': 'someone', 'resource_conf': {}, 'volume_confs': [],
        'input_confs': list(input_confs), 'output_confs': list(output_confs), 'pulsar_operations': pulsar,
        'task_config': {'inputs_directory': '/pulsar/abc/inputs', 'job_directory': '/pulsar/abc'},
    }
    asyncio.run(event_actions.handle_run_task(('run_task', payload)))
    return task, pulsar
