"""Tasks outlive the tesp-api process waiting for them (issue #52).

A shutdown (asyncio.CancelledError) leaves the task and its Pulsar job alone; leases tell which process works on
which task, and an orphaned task is taken over by one process: a submitted Pulsar job is waited for again, a task
that never reached Pulsar ends in SYSTEM_ERROR.

The real task repository imports motor 2.5, which needs Python < 3.11, so the tests run against an in-memory
collection that understands the few query and update operators tesp-api uses.
"""
import asyncio
import datetime
import importlib
import sys
import types

import pytest
from bson.objectid import ObjectId
from pymonad.maybe import Nothing
from pymonad.promise import Promise

from tesp_api.repository.model.task import RegisteredTesTask, TesTaskState
from tesp_api.utils.functional import maybe_of

NOW = datetime.datetime.now(datetime.timezone.utc)


# --- In-memory task collection -----------------------------------------------------------------------------------------

def _get(doc, dotted):
    for key in dotted.split('.'):
        if not isinstance(doc, dict) or key not in doc:
            return _MISSING
        doc = doc[key]
    return doc


_MISSING = object()


def _matches_value(value, condition):
    if isinstance(condition, dict) and condition and all(k.startswith('$') for k in condition):
        for op, arg in condition.items():
            if op == '$exists':
                if (value is not _MISSING) != arg:
                    return False
            elif value is _MISSING:
                return op in ('$ne', '$nin')
            elif op == '$in' and value not in arg:
                return False
            elif op == '$nin' and value in arg:
                return False
            elif op == '$ne' and value == arg:
                return False
            elif op == '$lt' and not value < arg:
                return False
        return True
    return value is not _MISSING and value == condition


def _matches(doc, query):
    for key, condition in query.items():
        if key == '$or':
            if not any(_matches(doc, alternative) for alternative in condition):
                return False
        elif not _matches_value(_get(doc, key), condition):
            return False
    return True


def _apply(doc, update):
    for dotted, value in update.get('$set', {}).items():
        *parents, last = dotted.split('.')
        target = doc
        for key in parents:
            target = target.setdefault(key, {})
        target[last] = value
    for dotted in update.get('$unset', {}):
        doc.pop(dotted, None)
    for dotted, value in update.get('$push', {}).items():
        assert dotted == 'logs.$[].system_logs'
        for log in doc['logs']:
            log.setdefault('system_logs', []).append(value)


def _resolved(value):
    return Promise(lambda resolve, reject: resolve(value))


class FakeTaskRepository:
    def __init__(self):
        self.docs = []

    def add(self, state, _id=None, **fields):
        doc = {'_id': _id or ObjectId(), 'state': state, 'author': None,
               'executors': [{'image': 'ubuntu', 'command': ['true']}],
               'logs': [{'logs': [], 'outputs': [], 'system_logs': [], 'start_time': NOW}], **fields}
        self.docs.append(doc)
        return doc

    def _find(self, query):
        return next((doc for doc in self.docs if _matches(doc, query)), None)

    def update_raw_task(self, search_query, update_query):
        doc = self._find(search_query)
        if doc is not None:
            _apply(doc, update_query)
        return _resolved(maybe_of(doc))

    def update_task(self, search_query, update_query):
        return self.update_raw_task(search_query, update_query) \
            .map(lambda found: found.map(lambda doc: RegisteredTesTask(**doc)))

    def update_tasks(self, search_query, update_query):
        found = [doc for doc in self.docs if _matches(doc, search_query)]
        for doc in found:
            _apply(doc, update_query)
        return _resolved(len(found))

    def update_task_state(self, task_id, old_state, new_state):
        return self.update_task({'_id': task_id, 'state': old_state}, {'$set': {'state': new_state}})

    def get_task(self, author, search_query):
        query = {**author.maybe({}, lambda a: {'author': a}), **search_query}
        return _resolved(maybe_of(self._find(query)).map(lambda doc: RegisteredTesTask(**doc)))


class FakeOperations:
    """Pulsar answering ``job_status`` (or each of ``statuses`` in turn, polled as tesp-api does) and ``files`` from
    the job's working directory."""

    def __init__(self, job_status=None, statuses=(), files=None):
        self.erased = []
        self.job_status = job_status
        self.statuses = list(statuses)
        self.files = files or {}
        self.states_seen = []  # the task's state at each status request

    def erase_job(self, job_id):
        self.erased.append(str(job_id))
        return _resolved(None)

    async def read_job_file(self, job_id, name, tail_bytes=None):
        return self.files.get(name)

    async def job_status_complete(self, job_id, is_canceled, on_started=None):
        if isinstance(self.job_status, BaseException):
            raise self.job_status
        if not self.statuses:
            if on_started is not None:
                await on_started()  # as poll_until_complete: the complete status says the job has started
            return self.job_status
        from tesp_api.service.job_polling import StatusPolling, poll_until_complete
        from tesp_api.service.task_recovery import task_repository

        async def fetch_status():
            task = (await task_repository.get_task(Nothing, {'_id': ObjectId(job_id)})).value
            self.states_seen.append(task.state)
            return self.statuses.pop(0)

        async def no_sleep(seconds):
            pass

        return await poll_until_complete(fetch_status, is_canceled, StatusPolling(), job_id=job_id, sleep=no_sleep,
                                         on_started=on_started)


@pytest.fixture
def repo(monkeypatch):
    """A fresh repository; the modules using it are imported anew against it and dropped after the test."""
    fake = FakeTaskRepository()
    module = types.ModuleType('tesp_api.repository.task_repository')
    module.task_repository = fake
    monkeypatch.setitem(sys.modules, 'tesp_api.repository.task_repository', module)
    for name in ('tesp_api.service.error', 'tesp_api.repository.task_repository_utils',
                 'tesp_api.service.task_recovery', 'tesp_api.service.event_actions'):
        monkeypatch.setitem(sys.modules, name, None)  # restored after the test
        del sys.modules[name]
    return fake


@pytest.fixture
def recovery(repo, monkeypatch):
    module = importlib.import_module('tesp_api.service.task_recovery')
    module.dispatched = []
    monkeypatch.setattr(module, 'dispatch_event', lambda name, payload: module.dispatched.append((name, payload)))
    return module


def _import_event_actions():
    async def load():  # pulsar_service, imported by event_actions, needs a running event loop
        return importlib.import_module('tesp_api.service.event_actions')
    return asyncio.run(load())


def _lease(owner, seconds_from_now):
    return {'owner': owner, 'expires': NOW + datetime.timedelta(seconds=seconds_from_now)}


def _old_id():
    return ObjectId.from_datetime(NOW - datetime.timedelta(hours=1))


# --- Shutdown -------------------------------------------------------------------------------------------------------

def test_shutdown_while_polling_leaves_task_and_pulsar_job_alone(repo, recovery):
    event_actions = _import_event_actions()
    task = repo.add(TesTaskState.RUNNING, tesp_pulsar_job_submitted=True, tesp_lease=_lease(recovery.INSTANCE_ID, 60))
    operations = FakeOperations(job_status=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(event_actions.handle_resume_task(
            ('resume_task', {'task_id': task['_id'], 'author': None, 'pulsar_operations': operations})))

    assert task['state'] == TesTaskState.RUNNING  # not CANCELED: no client canceled it
    assert operations.erased == []  # the Pulsar job goes on
    assert task['tesp_lease']['owner'] == recovery.INSTANCE_ID  # handed over by let_go / lease expiry
    assert task['logs'][0]['logs'] == []


def test_let_go_expires_only_this_process_leases(repo, recovery):
    own = repo.add(TesTaskState.RUNNING, tesp_lease=_lease(recovery.INSTANCE_ID, 120))
    other = repo.add(TesTaskState.RUNNING, tesp_lease=_lease('other', 120))
    asyncio.run(recovery.let_go())
    assert own['tesp_lease']['expires'] <= datetime.datetime.now(datetime.timezone.utc)
    assert other['tesp_lease']['expires'] == NOW + datetime.timedelta(seconds=120)


# --- Leases ---------------------------------------------------------------------------------------------------------

def test_renew_extends_own_leases_of_active_tasks_only(repo, recovery):
    own = repo.add(TesTaskState.RUNNING, tesp_lease=_lease(recovery.INSTANCE_ID, 10))
    finished = repo.add(TesTaskState.COMPLETE, tesp_lease=_lease(recovery.INSTANCE_ID, 10))
    other = repo.add(TesTaskState.RUNNING, tesp_lease=_lease('other', 10))
    asyncio.run(recovery.renew())
    assert own['tesp_lease']['expires'] > NOW + datetime.timedelta(seconds=recovery.LEASE_SECONDS - 5)
    assert finished['tesp_lease']['expires'] == other['tesp_lease']['expires'] == NOW + datetime.timedelta(seconds=10)


def test_take_and_release(repo, recovery):
    task = repo.add(TesTaskState.QUEUED)
    asyncio.run(recovery.take(task['_id']))
    assert task['tesp_lease']['owner'] == recovery.INSTANCE_ID
    asyncio.run(recovery.release(task['_id']))
    assert 'tesp_lease' in task  # still active: kept, so the task is not taken for orphaned
    task['state'] = TesTaskState.COMPLETE
    asyncio.run(recovery.release(task['_id']))
    assert 'tesp_lease' not in task


@pytest.mark.parametrize('state, fields, orphaned', [
    (TesTaskState.RUNNING, {'tesp_lease': _lease('gone', -1)}, True),
    (TesTaskState.QUEUED, {'tesp_lease': _lease('gone', -1)}, True),
    (TesTaskState.INITIALIZING, {'tesp_lease': _lease('gone', -1)}, True),
    (TesTaskState.CANCELED, {'tesp_lease': _lease('gone', -1)}, True),
    (TesTaskState.RUNNING, {'tesp_lease': _lease('alive', 60)}, False),
    (TesTaskState.COMPLETE, {'tesp_lease': _lease('gone', -1)}, False),
    (TesTaskState.EXECUTOR_ERROR, {'tesp_lease': _lease('gone', -1)}, False),
    (TesTaskState.CANCELED, {}, False),  # canceled and handled
    (TesTaskState.RUNNING, {'_id': _old_id()}, True),  # stored by a tesp-api without leases
    (TesTaskState.QUEUED, {}, False),  # just created, not claimed yet by the process that created it
])
def test_which_tasks_are_orphaned(repo, recovery, state, fields, orphaned):
    task = repo.add(state, **fields)
    claimed = asyncio.run(recovery.claim_orphan())
    assert (claimed is task) == orphaned
    if orphaned:
        assert task['tesp_lease']['owner'] == recovery.INSTANCE_ID


def test_a_claimed_task_is_not_claimed_again(repo, recovery, monkeypatch):
    repo.add(TesTaskState.RUNNING, tesp_lease=_lease('gone', -1))
    assert asyncio.run(recovery.claim_orphan()) is not None
    monkeypatch.setattr(recovery, 'INSTANCE_ID', 'another-worker')
    assert asyncio.run(recovery.claim_orphan()) is None


# --- Recovery -------------------------------------------------------------------------------------------------------

def test_submitted_running_task_is_resumed(repo, recovery):
    task = repo.add(TesTaskState.RUNNING, tesp_pulsar_job_submitted=True, tesp_lease=_lease('gone', -1),
                    author='alice')
    operations = FakeOperations()
    assert asyncio.run(recovery.sweep(lambda: operations)) == 1
    [(name, payload)] = recovery.dispatched
    assert name == 'resume_task'
    assert payload == {'task_id': task['_id'], 'author': 'alice', 'pulsar_operations': operations}
    assert task['state'] == TesTaskState.RUNNING
    assert operations.erased == []


def test_submitted_initializing_task_is_resumed(repo, recovery):
    # Its job was submitted and waits in the batch system's queue (issue #65): resumed, not ended.
    task = repo.add(TesTaskState.INITIALIZING, tesp_pulsar_job_submitted=True, tesp_lease=_lease('gone', -1))
    operations = FakeOperations()
    assert asyncio.run(recovery.sweep(lambda: operations)) == 1
    assert [name for name, _ in recovery.dispatched] == ['resume_task']
    assert task['state'] == TesTaskState.INITIALIZING
    assert operations.erased == []


def test_running_task_of_an_older_tesp_api_is_resumed(repo, recovery):
    # Stored before the submission mark: a RUNNING task's job was submitted (#65), so the wait is resumed.
    task = repo.add(TesTaskState.RUNNING, _id=_old_id())
    operations = FakeOperations()
    assert asyncio.run(recovery.sweep(lambda: operations)) == 1
    assert [name for name, _ in recovery.dispatched] == ['resume_task']
    assert task['state'] == TesTaskState.RUNNING and operations.erased == []


@pytest.mark.parametrize('state', [TesTaskState.QUEUED, TesTaskState.INITIALIZING])
def test_task_never_submitted_ends_in_system_error(repo, recovery, state):
    task = repo.add(state, tesp_lease=_lease('gone', -1))
    operations = FakeOperations()
    assert asyncio.run(recovery.sweep(lambda: operations)) == 1
    assert task['state'] == TesTaskState.SYSTEM_ERROR
    assert task['logs'][0]['system_logs'] == [recovery.NOT_RUN_SYSTEM_LOG]
    assert operations.erased == [str(task['_id'])]  # whatever setup_job left in Pulsar
    assert 'tesp_lease' not in task
    assert recovery.dispatched == []


def test_task_canceled_while_no_process_waited_gets_its_pulsar_job_canceled(repo, recovery):
    task = repo.add(TesTaskState.CANCELED, tesp_pulsar_job_submitted=True, tesp_lease=_lease('gone', -1))
    operations = FakeOperations()
    asyncio.run(recovery.sweep(lambda: operations))
    assert task['state'] == TesTaskState.CANCELED
    assert operations.erased == [str(task['_id'])]
    assert 'tesp_lease' not in task
    assert asyncio.run(recovery.sweep(lambda: operations)) == 0  # done once


def test_sweep_recovers_every_orphan_and_spares_live_tasks(repo, recovery):
    orphans = [repo.add(TesTaskState.QUEUED, tesp_lease=_lease('gone', -1)) for _ in range(3)]
    live = repo.add(TesTaskState.QUEUED, tesp_lease=_lease('alive', 60))
    assert asyncio.run(recovery.sweep(FakeOperations)) == 3
    assert all(task['state'] == TesTaskState.SYSTEM_ERROR for task in orphans)
    assert live['state'] == TesTaskState.QUEUED


def test_sweep_loop_survives_errors(repo, recovery, monkeypatch):
    sweeps = []

    async def failing_sweep(get_operations):
        sweeps.append(True)
        raise RuntimeError('mongo down')

    async def sleep(seconds):
        assert seconds == recovery.SWEEP_SECONDS
        if len(sweeps) == 3:
            raise asyncio.CancelledError()

    monkeypatch.setattr(recovery, 'sweep', failing_sweep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(recovery.run(FakeOperations, sleep=sleep))
    assert len(sweeps) == 3


# --- Resumed wait ---------------------------------------------------------------------------------------------------

def test_resumed_task_completes_with_its_executor_log(repo, recovery):
    event_actions = _import_event_actions()
    task = repo.add(TesTaskState.RUNNING, tesp_pulsar_job_submitted=True, tesp_lease=_lease(recovery.INSTANCE_ID, 60))
    operations = FakeOperations(job_status={'complete': 'true', 'returncode': 0, 'job_stdout': 'out\n',
                                            'job_stderr': 'err\n'})

    asyncio.run(event_actions.handle_resume_task(
        ('resume_task', {'task_id': task['_id'], 'author': None, 'pulsar_operations': operations})))

    assert task['state'] == TesTaskState.COMPLETE
    [executor_log] = task['logs'][0]['logs']
    assert (executor_log['stdout'], executor_log['stderr'], executor_log['exit_code']) == ('out\n', 'err\n', 0)
    assert operations.erased == [str(task['_id'])]
    assert 'tesp_lease' not in task


def test_resumed_task_failing_ends_in_executor_error(repo, recovery):
    event_actions = _import_event_actions()
    task = repo.add(TesTaskState.RUNNING, tesp_pulsar_job_submitted=True, tesp_lease=_lease(recovery.INSTANCE_ID, 60))
    operations = FakeOperations(job_status={'complete': 'true', 'returncode': 2})

    asyncio.run(event_actions.handle_resume_task(
        ('resume_task', {'task_id': task['_id'], 'author': None, 'pulsar_operations': operations})))

    assert task['state'] == TesTaskState.EXECUTOR_ERROR
    assert task['logs'][0]['logs'][0]['exit_code'] == 2


def test_task_queued_in_slurm_during_a_restart_survives_it(repo, recovery):
    # The process that submitted the job stopped while SLURM kept it pending; its lease expired. Another process
    # claims the task, resumes the wait, sets it RUNNING once the job runs, and ends it with per-executor logs.
    event_actions = _import_event_actions()
    task = repo.add(TesTaskState.INITIALIZING, tesp_pulsar_job_submitted=True, tesp_lease=_lease('gone', -1),
                    executors=[{'image': 'ubuntu', 'command': ['echo', 'a']},
                               {'image': 'ubuntu', 'command': ['echo', 'b']}])
    task['logs'][0]['start_time'] = None
    workdir = ['tesp-job-started', *(f'tesp-executor-{i}.{kind}' for i in (0, 1)
                                     for kind in ('stdout', 'stderr', 'status'))]
    files = {'tesp-executor-0.stdout': b'a\n', 'tesp-executor-0.status': b'1700000000 1700000005 0',
             'tesp-executor-1.stdout': b'b\n', 'tesp-executor-1.status': b'1700000005 1700000009 0'}
    operations = FakeOperations(files=files, statuses=[
        {'complete': 'false', 'status': 'queued'},
        {'complete': 'false', 'status': 'queued'},
        {'complete': 'false', 'status': 'running'},
        {'complete': 'true', 'status': 'complete', 'returncode': 0, 'job_stdout': '', 'job_stderr': '',
         'working_directory_contents': workdir}])
    monkeypatch_dispatch = []
    recovery.dispatch_event = lambda name, payload: monkeypatch_dispatch.append((name, payload))

    assert asyncio.run(recovery.sweep(lambda: operations)) == 1
    [(name, payload)] = monkeypatch_dispatch
    assert task['state'] == TesTaskState.INITIALIZING  # not ended by the recovery
    asyncio.run(event_actions.handle_resume_task((name, payload)))

    assert operations.states_seen == [TesTaskState.INITIALIZING, TesTaskState.INITIALIZING,
                                      TesTaskState.INITIALIZING, TesTaskState.RUNNING]
    assert task['state'] == TesTaskState.COMPLETE
    assert task['logs'][0]['start_time'] is not None  # set when the job left the queue
    assert [(log['stdout'], log['exit_code']) for log in task['logs'][0]['logs']] == [('a\n', 0), ('b\n', 0)]
    assert operations.erased == [str(task['_id'])]
    assert 'tesp_lease' not in task


def test_resumed_task_whose_stage_out_failed_ends_in_system_error(repo, recovery):
    # The chain's finish logic (#83/#84) applies to a resumed wait too.
    event_actions = _import_event_actions()
    task = repo.add(TesTaskState.RUNNING, tesp_pulsar_job_submitted=True, tesp_lease=_lease(recovery.INSTANCE_ID, 60))
    operations = FakeOperations(job_status={
        'complete': 'true', 'status': 'complete', 'returncode': 6, 'job_stdout': '', 'job_stderr': 'upload refused',
        'working_directory_contents': ['tesp-job-started', 'tesp-stage-out-failed', 'tesp-executor-0.status']},
        files={'tesp-executor-0.status': b'1700000000 1700000005 0'})

    asyncio.run(event_actions.handle_resume_task(
        ('resume_task', {'task_id': task['_id'], 'author': None, 'pulsar_operations': operations})))

    assert task['state'] == TesTaskState.SYSTEM_ERROR
    assert [log['exit_code'] for log in task['logs'][0]['logs']] == [0]
    assert any(log.startswith('stage-out failed') for log in task['logs'][0]['system_logs'])


def test_resumed_running_task_keeps_its_start_time(repo, recovery):
    event_actions = _import_event_actions()
    started = NOW - datetime.timedelta(hours=2)
    task = repo.add(TesTaskState.RUNNING, tesp_pulsar_job_submitted=True, tesp_lease=_lease(recovery.INSTANCE_ID, 60))
    task['logs'][0]['start_time'] = started
    operations = FakeOperations(statuses=[{'complete': 'false', 'status': 'running'},
                                          {'complete': 'true', 'status': 'complete', 'returncode': 0,
                                           'job_stdout': 'out\n', 'job_stderr': ''}])
    asyncio.run(event_actions.handle_resume_task(
        ('resume_task', {'task_id': task['_id'], 'author': None, 'pulsar_operations': operations})))
    assert task['state'] == TesTaskState.COMPLETE
    assert operations.states_seen == [TesTaskState.RUNNING, TesTaskState.RUNNING]
    [executor_log] = task['logs'][0]['logs']
    assert executor_log['start_time'] == started.isoformat()  # older Pulsar: one log for the job, from its start


def test_task_canceled_during_a_resumed_wait_gets_its_job_canceled(repo, recovery):
    event_actions = _import_event_actions()
    task = repo.add(TesTaskState.INITIALIZING, tesp_pulsar_job_submitted=True,
                    tesp_lease=_lease(recovery.INSTANCE_ID, 60))
    operations = FakeOperations(statuses=[{'complete': 'false', 'status': 'queued'}] * 5)
    original = operations.job_status_complete

    async def cancel_then_wait(job_id, is_canceled, on_started=None):
        task['state'] = TesTaskState.CANCELED  # what POST /tasks/{id}:cancel does
        return await original(job_id, is_canceled, on_started)

    operations.job_status_complete = cancel_then_wait
    asyncio.run(event_actions.handle_resume_task(
        ('resume_task', {'task_id': task['_id'], 'author': None, 'pulsar_operations': operations})))
    assert task['state'] == TesTaskState.CANCELED
    assert operations.erased == [str(task['_id'])]
    assert 'tesp_lease' not in task


def test_process_that_lost_its_lease_stops_waiting(repo, recovery):
    # This process stalled past its lease and another one claimed the task: this one must not finish it too.
    event_actions = _import_event_actions()
    task = repo.add(TesTaskState.RUNNING, tesp_pulsar_job_submitted=True, tesp_lease=_lease('other', 60))
    operations = FakeOperations(statuses=[{'complete': 'false', 'status': 'running'},
                                          {'complete': 'true', 'status': 'complete', 'returncode': 0}])
    asyncio.run(event_actions.handle_resume_task(
        ('resume_task', {'task_id': task['_id'], 'author': None, 'pulsar_operations': operations})))
    assert task['state'] == TesTaskState.RUNNING
    assert task['logs'][0]['logs'] == [] and operations.erased == []
    assert task['tesp_lease']['owner'] == 'other'  # left to the process that holds it
    assert operations.states_seen == []  # stopped before the first status request


def test_lease_lost_while_the_job_ran_is_noticed_before_finishing(repo, recovery):
    event_actions = _import_event_actions()
    task = repo.add(TesTaskState.RUNNING, tesp_pulsar_job_submitted=True, tesp_lease=_lease(recovery.INSTANCE_ID, 60))
    operations = FakeOperations(job_status={'complete': 'true', 'returncode': 0})
    original = operations.job_status_complete

    async def taken_over_meanwhile(job_id, is_canceled, on_started=None):
        task['tesp_lease'] = _lease('other', 60)
        return await original(job_id, is_canceled, on_started)

    operations.job_status_complete = taken_over_meanwhile
    asyncio.run(event_actions.handle_resume_task(
        ('resume_task', {'task_id': task['_id'], 'author': None, 'pulsar_operations': operations})))
    assert task['state'] == TesTaskState.RUNNING and operations.erased == []


def test_task_whose_error_update_failed_is_left_for_the_next_sweep(repo, recovery, monkeypatch):
    task = repo.add(TesTaskState.QUEUED, tesp_lease=_lease('gone', -1))

    async def failing_update(*args, **kwargs):
        pass  # repo_update_error_task_promise logs and swallows database errors

    monkeypatch.setattr(recovery, 'repo_update_error_task_promise', failing_update)
    asyncio.run(recovery.sweep(FakeOperations))
    assert task['state'] == TesTaskState.QUEUED
    assert task['tesp_lease']['expires'] <= datetime.datetime.now(datetime.timezone.utc)  # not kept by this process


def test_run_task_marks_the_job_submitted(monkeypatch, tmp_path):
    # Without the mark, a restart would end a task whose job waits in SLURM's queue instead of resuming it.
    from tesp_api.repository.model.task import TesTaskExecutor
    from tests.unit.run_task_stub import run_task
    status = {'complete': 'true', 'status': 'complete', 'returncode': 0, 'job_stdout': '', 'job_stderr': ''}
    task, _ = run_task(monkeypatch, status, [TesTaskExecutor(image='ubuntu', command=['true'])])
    assert task.marks.get('tesp_pulsar_job_submitted') is True


def test_database_error_on_the_last_lease_renewal_still_finishes_the_task(repo, recovery, monkeypatch):
    event_actions = _import_event_actions()
    task = repo.add(TesTaskState.RUNNING, tesp_pulsar_job_submitted=True, tesp_lease=_lease(recovery.INSTANCE_ID, 60))
    operations = FakeOperations(job_status={'complete': 'true', 'returncode': 0})

    async def failing_hold(task_id):
        raise RuntimeError('mongo hiccup')

    monkeypatch.setattr(event_actions.task_recovery, 'hold', failing_hold)
    asyncio.run(event_actions.handle_resume_task(
        ('resume_task', {'task_id': task['_id'], 'author': None, 'pulsar_operations': operations})))
    assert task['state'] == TesTaskState.COMPLETE


def test_lease_renewed_twice_in_the_same_instant_is_still_held(repo, recovery, monkeypatch):
    task = repo.add(TesTaskState.RUNNING, tesp_lease=_lease(recovery.INSTANCE_ID, 60))
    monkeypatch.setattr(recovery, '_now', lambda: NOW)
    asyncio.run(recovery.hold(task['_id']))
    asyncio.run(recovery.hold(task['_id']))  # no change to the document, still this process's lease
