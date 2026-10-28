"""A task is RUNNING only once its Pulsar job runs (issue #65).

Before, `run_task` set the task RUNNING, with its start time, before it submitted the job, so a task whose SLURM job
was pending (behind other jobs, or on resources the cluster cannot give) showed RUNNING. Now the task stays
INITIALIZING while Pulsar reports the job `queued` (or `preprocessing`), and becomes RUNNING at the first status that
says the job left the queue.

The real task repository imports motor 2.5, which needs Python < 3.11, so the handler tests run against an in-memory
repository.
"""
import asyncio
import importlib
import sys
import types

import pytest
from bson.objectid import ObjectId
from pymonad.promise import Promise

from tesp_api.repository.model.task import RegisteredTesTask, TesTaskState
from tesp_api.service.job_polling import StatusPolling, TaskCanceledError, job_started, poll_until_complete
from tesp_api.utils.functional import maybe_of

PREPROCESSING = {'complete': 'false', 'status': 'preprocessing'}
QUEUED = {'complete': 'false', 'status': 'queued'}
RUNNING = {'complete': 'false', 'status': 'running'}
POSTPROCESSING = {'complete': 'false', 'status': 'postprocessing'}
DONE = {'complete': 'true', 'status': 'complete', 'returncode': 0, 'job_stdout': 'hello\n', 'job_stderr': ''}
FAILED = {'complete': 'true', 'status': 'complete', 'returncode': 3, 'job_stdout': '', 'job_stderr': 'boom\n'}


async def _no_sleep(seconds):
    pass


async def _never_canceled():
    return False


# --- Poll loop ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('status, started', [
    (PREPROCESSING, False),
    (QUEUED, False),
    (RUNNING, True),
    (POSTPROCESSING, True),
    (DONE, True),
    ({'complete': 'true', 'status': 'failed', 'returncode': None}, True),
    ({'complete': 'true', 'status': 'cancelled', 'returncode': None}, True),
    ({'complete': 'false'}, True),  # no status reported: as before, the job counts as running
])
def test_job_started(status, started):
    assert job_started(status) is started


def _poll(answers, is_canceled=_never_canceled):
    """Polls scripted statuses (the last repeats); returns the result, the poll count at each on_started call and
    the number of polls."""
    answers = list(answers)
    polls = []
    started_at = []

    async def fetch():
        polls.append(1)
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def on_started():
        started_at.append(len(polls))

    result = asyncio.run(poll_until_complete(fetch, is_canceled, StatusPolling(), sleep=_no_sleep,
                                             on_started=on_started))
    return result, started_at, len(polls)


def test_on_started_waits_for_the_job_to_leave_the_queue():
    result, started_at, polls = _poll([PREPROCESSING, QUEUED, QUEUED, RUNNING, RUNNING, DONE])
    assert result == DONE
    assert started_at == [4]  # once, at the first `running`
    assert polls == 6


def test_on_started_runs_for_a_job_that_finished_between_two_requests():
    result, started_at, _ = _poll([QUEUED, DONE])
    assert result == DONE
    assert started_at == [2]


def test_on_started_does_not_run_for_a_job_canceled_in_the_queue():
    checks = []
    started = []

    async def fetch():
        return QUEUED

    async def canceled_after_two_polls():
        checks.append(1)
        return len(checks) > 2

    async def on_started():
        started.append(1)

    with pytest.raises(TaskCanceledError):
        asyncio.run(poll_until_complete(fetch, canceled_after_two_polls, StatusPolling(), sleep=_no_sleep,
                                        on_started=on_started))
    assert started == []


def test_on_started_is_optional():
    async def fetch():
        return DONE
    assert asyncio.run(poll_until_complete(fetch, _never_canceled, StatusPolling(), sleep=_no_sleep)) == DONE


# --- run_task handler ---------------------------------------------------------------------------------------------------

def _resolved(value):
    return Promise(lambda resolve, reject: resolve(value))


def _matches(doc, query):
    return all(doc.get(key) == value for key, value in query.items())


class FakeTaskRepository:
    """The task repository calls `run_task` makes, on dicts; updates support `$set` only."""

    def __init__(self):
        self.docs = []

    def add(self, state):
        doc = {'_id': ObjectId(), 'state': state, 'author': None,
               'executors': [{'image': 'ubuntu', 'command': ['echo', 'hello']}],
               'logs': [{'logs': [], 'outputs': [], 'system_logs': []}]}
        self.docs.append(doc)
        return doc

    def _find(self, query):
        return next((doc for doc in self.docs if _matches(doc, query)), None)

    def update_task(self, search_query, update_query):
        doc = self._find(search_query)
        if doc is not None:
            doc.update(update_query['$set'])
        return _resolved(maybe_of(doc).map(lambda found: RegisteredTesTask(**found)))

    def update_task_state(self, task_id, old_state, new_state):
        query = {'_id': task_id} if old_state is None else {'_id': task_id, 'state': old_state}
        return self.update_task(query, {'$set': {'state': new_state}})

    def get_task(self, author, search_query):
        query = {**author.maybe({}, lambda a: {'author': a}), **search_query}
        return _resolved(maybe_of(self._find(query)).map(lambda doc: RegisteredTesTask(**doc)))


class FakeOperations:
    """Answers status requests from a script (the last entry repeats) and records the task's state at each one."""

    def __init__(self, repo, task, answers, cancel_at_poll=None):
        self.repo, self.task = repo, task
        self.answers = list(answers)
        self.cancel_at_poll = cancel_at_poll
        self.states_seen = []
        self.submitted = []
        self.erased = []

    def run_job(self, job_id, run_command, submit_params=None):
        self.submitted.append(str(job_id))
        return _resolved(b'')

    def erase_job(self, job_id):
        self.erased.append(str(job_id))
        return _resolved(None)

    async def job_status_complete(self, job_id, is_canceled, on_started=None):
        async def fetch():
            self.states_seen.append(self.task['state'])
            if len(self.states_seen) == self.cancel_at_poll:
                self.task['state'] = TesTaskState.CANCELED  # what POST /tasks/{id}:cancel does
            return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return await poll_until_complete(fetch, is_canceled, StatusPolling(), job_id=job_id, sleep=_no_sleep,
                                         on_started=on_started)


@pytest.fixture
def repo(monkeypatch):
    """A fresh repository; the modules using it are imported anew against it and dropped after the test."""
    fake = FakeTaskRepository()
    module = types.ModuleType('tesp_api.repository.task_repository')
    module.task_repository = fake
    monkeypatch.setitem(sys.modules, 'tesp_api.repository.task_repository', module)
    for name in ('tesp_api.service.error', 'tesp_api.repository.task_repository_utils',
                 'tesp_api.service.event_actions'):
        monkeypatch.setitem(sys.modules, name, None)  # restored after the test
        del sys.modules[name]
    return fake


def _run_task(repo, task, answers, cancel_at_poll=None):
    operations = FakeOperations(repo, task, answers, cancel_at_poll)

    async def run():  # pulsar_service, imported by event_actions, needs a running event loop
        event_actions = importlib.import_module('tesp_api.service.event_actions')
        await event_actions.handle_run_task(('run_task', {
            'task_id': task['_id'], 'author': None, 'pulsar_operations': operations,
            'task_config': {'inputs_directory': '/pulsar/inputs'},
            'resource_conf': {'cpu_cores': None, 'ram_gb': None},
            'volume_confs': [], 'input_confs': [], 'output_confs': [],
        }))

    asyncio.run(run())
    return operations


def test_task_stays_initializing_while_its_job_is_queued(repo):
    task = repo.add(TesTaskState.INITIALIZING)
    operations = _run_task(repo, task, [QUEUED, QUEUED, QUEUED, RUNNING, RUNNING, DONE])

    assert operations.submitted == [str(task['_id'])]
    assert operations.states_seen == [TesTaskState.INITIALIZING] * 4 + [TesTaskState.RUNNING] * 2
    assert task['state'] == TesTaskState.COMPLETE
    log = task['logs'][-1]
    executor_log = log['logs'][-1]
    assert executor_log['stdout'] == 'hello\n'
    assert executor_log['exit_code'] == 0
    # The executor and the task log start when the job left the queue.
    assert log['start_time'] is not None
    assert executor_log['start_time'] == log['start_time']


def test_job_that_finished_between_two_requests_still_passes_through_running(repo):
    task = repo.add(TesTaskState.INITIALIZING)
    operations = _run_task(repo, task, [QUEUED, FAILED])

    assert operations.states_seen == [TesTaskState.INITIALIZING] * 2
    assert task['state'] == TesTaskState.EXECUTOR_ERROR
    executor_log = task['logs'][-1]['logs'][-1]
    assert executor_log['exit_code'] == 3
    assert executor_log['start_time'] == task['logs'][-1]['start_time']


def test_task_canceled_while_queued_never_runs_and_its_job_is_canceled(repo):
    task = repo.add(TesTaskState.INITIALIZING)
    operations = _run_task(repo, task, [QUEUED], cancel_at_poll=3)

    assert task['state'] == TesTaskState.CANCELED
    assert TesTaskState.RUNNING not in operations.states_seen
    assert operations.erased == [str(task['_id'])]
    assert task['logs'][-1]['logs'] == []


def test_task_canceled_before_submission_is_not_submitted(repo):
    task = repo.add(TesTaskState.CANCELED)
    operations = _run_task(repo, task, [DONE])

    assert operations.submitted == []
    assert task['state'] == TesTaskState.CANCELED
