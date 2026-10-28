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
    def __init__(self, job_status=None):
        self.erased = []
        self.job_status = job_status

    def erase_job(self, job_id):
        self.erased.append(str(job_id))
        return _resolved(None)

    async def job_status_complete(self, job_id, is_canceled):
        if isinstance(self.job_status, BaseException):
            raise self.job_status
        return self.job_status


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


@pytest.mark.parametrize('state', [TesTaskState.QUEUED, TesTaskState.INITIALIZING, TesTaskState.RUNNING])
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
    task = repo.add(TesTaskState.RUNNING, tesp_pulsar_job_submitted=True)
    operations = FakeOperations(job_status={'complete': 'true', 'returncode': 2})

    asyncio.run(event_actions.handle_resume_task(
        ('resume_task', {'task_id': task['_id'], 'author': None, 'pulsar_operations': operations})))

    assert task['state'] == TesTaskState.EXECUTOR_ERROR
    assert task['logs'][0]['logs'][0]['exit_code'] == 2
