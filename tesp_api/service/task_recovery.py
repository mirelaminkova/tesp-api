"""
Tasks outlive the tesp-api process that waits for them (issue #52).

Each tesp-api process (every gunicorn worker) holds a lease on the tasks it works on: ``tesp_lease`` in the task
document names the process and when the lease expires. The process renews its leases every ``SWEEP_SECONDS``; a
process that stops, cleanly or not, stops renewing them, and a clean shutdown lets them expire at once. In the same
sweep, every process looks for tasks whose lease has expired and claims them one by one with an atomic update, so
only one process takes over a given task:

* ``RUNNING`` with its Pulsar job submitted (``tesp_pulsar_job_submitted``): waiting for the job is resumed, and the
  task ends as usual, with its executor logs. The Pulsar job id is the task id.
* ``QUEUED``, ``INITIALIZING`` or ``RUNNING`` without a submitted job: the task was never run. It is set to
  ``SYSTEM_ERROR`` with a system log, and whatever Pulsar holds for it is erased. It is not resubmitted, as its inputs
  may have been staged in part.
* ``CANCELED`` with a lease left: the client canceled it while no process was there to end its Pulsar job, which is
  done now.
"""
import asyncio
import datetime
import os
import socket
import uuid
from typing import Callable, Optional

from bson.objectid import ObjectId
from loguru import logger
from pymonad.maybe import Just

from tesp_api.repository.model.task import TesTaskState
from tesp_api.repository.task_repository import task_repository
from tesp_api.service.error import pulsar_cancel_task_promise, repo_update_error_task_promise
from tesp_api.service.event_dispatcher import dispatch_event
from tesp_api.service.pulsar_operations import PulsarOperations

LEASE_FIELD = 'tesp_lease'
SUBMITTED_FIELD = 'tesp_pulsar_job_submitted'
# A lease outlives three missed renewals, so a busy event loop does not lose its tasks to another process.
SWEEP_SECONDS = 45
LEASE_SECONDS = 4 * SWEEP_SECONDS
# Orphaned tasks claimed per sweep and process; the rest are left for the next sweep or another process.
MAX_CLAIMS_PER_SWEEP = 100
ACTIVE_STATES = [TesTaskState.QUEUED, TesTaskState.INITIALIZING, TesTaskState.RUNNING]
NOT_RUN_SYSTEM_LOG = ('tesp-api stopped before the task was submitted to Pulsar; the task was not run. '
                      'Submit it again.')

INSTANCE_ID = f'{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}'


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _lease(now: datetime.datetime) -> dict:
    return {LEASE_FIELD: {'owner': INSTANCE_ID, 'expires': now + datetime.timedelta(seconds=LEASE_SECONDS)}}


def orphaned_query(now: datetime.datetime) -> dict:
    """Tasks no living process works on."""
    expired = {f'{LEASE_FIELD}.expires': {'$lt': now}}
    return {'$or': [
        {'state': {'$in': ACTIVE_STATES}, **expired},
        # Stored by an older tesp-api, before leases. A younger task without a lease may be one the process that
        # created it has not claimed yet.
        {'state': {'$in': ACTIVE_STATES}, LEASE_FIELD: {'$exists': False},
         '_id': {'$lt': ObjectId.from_datetime(now - datetime.timedelta(seconds=LEASE_SECONDS))}},
        {'state': TesTaskState.CANCELED, **expired},
    ]}


def own_leases_query() -> dict:
    return {f'{LEASE_FIELD}.owner': INSTANCE_ID}


async def take(task_id: ObjectId) -> None:
    """Leases a task to this process; called before working on it."""
    await task_repository.update_tasks({'_id': task_id}, {'$set': _lease(_now())})


async def mark_submitted(task_id: ObjectId) -> None:
    """Records that the task's Pulsar job was submitted, so waiting for it can be resumed."""
    await task_repository.update_tasks({'_id': task_id}, {'$set': {SUBMITTED_FIELD: True}})


async def release(task_id: ObjectId) -> None:
    """Drops the lease of a task that reached a final state."""
    await task_repository.update_tasks(
        {'_id': task_id, 'state': {'$nin': ACTIVE_STATES}}, {'$unset': {LEASE_FIELD: ''}})


async def renew() -> None:
    await task_repository.update_tasks(
        {**own_leases_query(), 'state': {'$in': ACTIVE_STATES}},
        {'$set': {f'{LEASE_FIELD}.expires': _now() + datetime.timedelta(seconds=LEASE_SECONDS)}})


async def let_go() -> None:
    """On shutdown: lets this process's leases expire now, so another process takes the tasks over at once."""
    await task_repository.update_tasks(own_leases_query(), {'$set': {f'{LEASE_FIELD}.expires': _now()}})


async def claim_orphan() -> Optional[dict]:
    """Leases one orphaned task to this process and returns it as stored, or ``None`` when there is none."""
    now = _now()
    claimed = await task_repository.update_raw_task(orphaned_query(now), {'$set': _lease(now)})
    return claimed.maybe(None, lambda task: task)


async def recover(task: dict, pulsar_operations: PulsarOperations) -> None:
    task_id: ObjectId = task['_id']
    state = task.get('state')
    if state == TesTaskState.RUNNING and task.get(SUBMITTED_FIELD):
        logger.info(f'Resuming the wait for the Pulsar job of task {task_id}, left by a stopped tesp-api process.')
        dispatch_event('resume_task', {'task_id': task_id, 'author': task.get('author'),
                                       'pulsar_operations': pulsar_operations})
        return
    if state == TesTaskState.CANCELED:
        logger.info(f'Task {task_id} was canceled while no tesp-api process waited for it. Canceling its Pulsar job.')
    else:
        logger.warning(f'Task {task_id} was left {state} by a stopped tesp-api process before its Pulsar job was '
                       f'submitted. Setting it to SYSTEM_ERROR.')
        await repo_update_error_task_promise(task_id, TesTaskState.SYSTEM_ERROR, Just(NOT_RUN_SYSTEM_LOG),
                                             unless_canceled=True)
    await pulsar_cancel_task_promise(str(task_id), pulsar_operations)
    await release(task_id)


async def sweep(get_operations: Callable[[], PulsarOperations]) -> int:
    """Renews this process's leases and recovers orphaned tasks; returns how many were recovered."""
    await renew()
    recovered = 0
    while recovered < MAX_CLAIMS_PER_SWEEP:
        task = await claim_orphan()
        if task is None:
            break
        recovered += 1
        try:
            await recover(task, get_operations())
        except Exception as error:
            # The lease is this process's now; the task is retried once the lease expires.
            logger.error(f'Could not recover task {task["_id"]} [{type(error).__name__}: {error}]')
    return recovered


async def run(get_operations: Callable[[], PulsarOperations], sleep=asyncio.sleep) -> None:
    """Sweeps every ``SWEEP_SECONDS``, from startup on, until canceled."""
    while True:
        try:
            await sweep(get_operations)
        except Exception as error:
            logger.error(f'Task lease sweep failed [{type(error).__name__}: {error}]')
        await sleep(SWEEP_SECONDS)
