"""Waiting for a Pulsar job (issue #49): poll until the job is complete, however long it runs, with a growing interval,
an optional cap that ends in SYSTEM_ERROR, retries of transient Pulsar errors and a stop when the task is canceled.

The old loop gave up after `pulsar.status.max_polls` × `pulsar.status.poll_interval` (400 × 4 s ≈ 27 min) and the task
failed while its job kept running.
"""
import asyncio

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from loguru import logger
from multidict import CIMultiDict, CIMultiDictProxy
from pymonad.maybe import Just
from pymonad.promise import Promise
from yarl import URL

import tesp_api.service.error as error_module
from tesp_api.repository.model.task import TesTaskState
from tesp_api.service.error import pulsar_event_handle_error
from tesp_api.service.job_polling import (
    POLL_INTERVAL_GROWTH, PulsarJobTimeoutError, StatusPolling, TaskCanceledError, load_status_polling,
    poll_until_complete)
from tesp_api.service.pulsar_operations import (
    PulsarAmqpOperations, PulsarLayerConnectionError, PulsarOperationsError, PulsarRestOperations,
    is_transient_pulsar_error)

RUNNING = {'complete': 'false', 'status': 'running'}
DONE = {'complete': 'true', 'status': 'complete', 'returncode': 0}


class FakeClock:
    """Time that only moves when the poll loop sleeps."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def time(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _connection_error():
    return PulsarLayerConnectionError(aiohttp.ClientConnectionError('Connection reset by peer'))


def _http_error(status):
    url = URL('http://pulsar/jobs/job1/status')
    request_info = aiohttp.RequestInfo(url=url, method='GET', headers=CIMultiDictProxy(CIMultiDict()), real_url=url)
    return PulsarLayerConnectionError(aiohttp.ClientResponseError(request_info, (), status=status, message='err'))


class FakePulsar:
    """Answers status requests from a script: a dict is returned, an exception raised. The last entry repeats."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.polls = 0

    async def status(self):
        self.polls += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


async def _never_canceled():
    return False


def _poll(pulsar, polling=StatusPolling(), clock=None, is_canceled=_never_canceled):
    clock = clock or FakeClock()
    return asyncio.run(poll_until_complete(
        pulsar.status, is_canceled, polling, is_transient_pulsar_error, sleep=clock.sleep, clock=clock.time)), clock


# --- No fixed poll count ------------------------------------------------------------------------------------------

def test_job_completes_after_more_polls_than_the_old_limit():
    pulsar = FakePulsar(*([RUNNING] * 1000), DONE)
    status, clock = _poll(pulsar)
    assert status == DONE
    assert pulsar.polls == 1001  # the old loop stopped at max_polls = 400
    assert clock.now > 400 * 4


def test_job_completes_after_hours():
    pulsar = FakePulsar(*([RUNNING] * 600), DONE)
    status, clock = _poll(pulsar, StatusPolling(poll_interval=4, max_poll_interval=60))
    assert status == DONE
    assert clock.now > 9 * 3600


def test_first_poll_waits_the_base_interval():
    status, clock = _poll(FakePulsar(DONE), StatusPolling(poll_interval=4))
    assert clock.sleeps == [4]


# --- Interval grows to a cap ----------------------------------------------------------------------------------------

def test_interval_grows_to_the_cap():
    _, clock = _poll(FakePulsar(*([RUNNING] * 60), DONE), StatusPolling(poll_interval=4, max_poll_interval=60))
    assert clock.sleeps[0] == 4
    assert clock.sleeps[1] == pytest.approx(4 * POLL_INTERVAL_GROWTH)
    assert all(later >= earlier for earlier, later in zip(clock.sleeps, clock.sleeps[1:]))
    assert max(clock.sleeps) == 60
    assert clock.sleeps[-1] == 60


def test_interval_stays_fixed_when_the_cap_equals_the_base():
    _, clock = _poll(FakePulsar(*([RUNNING] * 10), DONE), StatusPolling(poll_interval=2, max_poll_interval=2))
    assert set(clock.sleeps) == {2}


# --- Transient Pulsar errors ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("error", [_connection_error(), _http_error(500), _http_error(502), _http_error(503),
                                   _http_error(429)])
def test_transient_errors_are_retried(error):
    pulsar = FakePulsar(RUNNING, error, error, RUNNING, DONE)
    status, _ = _poll(pulsar)
    assert status == DONE
    assert pulsar.polls == 5


@pytest.mark.parametrize("error", [_http_error(404), _http_error(400), PulsarOperationsError(ValueError('bad'))])
def test_other_errors_are_not_retried(error):
    pulsar = FakePulsar(RUNNING, error, DONE)
    with pytest.raises(type(error)):
        _poll(pulsar)
    assert pulsar.polls == 2


def test_persistent_transient_errors_give_up_after_the_retry_window():
    pulsar = FakePulsar(RUNNING, _connection_error())
    clock = FakeClock()
    with pytest.raises(PulsarLayerConnectionError):
        _poll(pulsar, StatusPolling(poll_interval=4, max_poll_interval=60, error_retry_seconds=600), clock)
    assert 600 <= clock.now <= 600 + 60 + 4 * 2  # first poll, then failing for the retry window and one interval
    assert pulsar.polls > 2


def test_retry_window_restarts_after_a_successful_poll():
    error = _connection_error()
    # Failing for 40 s, then a good poll, then failing for 40 s again: never 60 s in a row.
    answers = [RUNNING] + ([error] * 5 + [RUNNING]) * 4 + [DONE]
    status, _ = _poll(FakePulsar(*answers),
                      StatusPolling(poll_interval=10, max_poll_interval=10, error_retry_seconds=60))
    assert status == DONE


def test_zero_retry_window_fails_on_the_first_error():
    pulsar = FakePulsar(RUNNING, _connection_error(), DONE)
    with pytest.raises(PulsarLayerConnectionError):
        _poll(pulsar, StatusPolling(error_retry_seconds=0))
    assert pulsar.polls == 2


# --- Optional cap -----------------------------------------------------------------------------------------------------

def test_no_cap_by_default():
    assert StatusPolling().max_wait_seconds == 0


def test_cap_stops_polling():
    pulsar = FakePulsar(RUNNING)
    clock = FakeClock()
    with pytest.raises(PulsarJobTimeoutError) as raised:
        _poll(pulsar, StatusPolling(poll_interval=4, max_poll_interval=60, max_wait_seconds=3600), clock)
    assert 3600 <= clock.now < 3600 + 60
    assert raised.value.max_wait_seconds == 3600
    assert '3600' in str(raised.value)


def test_job_completing_at_the_cap_is_not_failed():
    pulsar = FakePulsar(RUNNING, DONE)
    status, _ = _poll(pulsar, StatusPolling(poll_interval=10, max_poll_interval=10, max_wait_seconds=20))
    assert status == DONE


def test_timeout_cancels_the_pulsar_job_and_records_system_error(monkeypatch):
    erased, updates = [], []

    class Operations:
        def erase_job(self, job_id):
            erased.append(job_id)
            return Promise(lambda resolve, reject: resolve(None))

    def update_task(query, update):
        updates.append((query, update))
        return Promise(lambda resolve, reject: resolve(Just(None)))

    monkeypatch.setattr(error_module.task_repository, 'update_task', update_task)
    async def handle():
        await pulsar_event_handle_error(PulsarJobTimeoutError(3600), 'abc', 'run_task', Operations())

    asyncio.run(handle())

    assert erased == ['abc']
    [(query, update)] = updates
    assert update['$set'] == {'state': TesTaskState.SYSTEM_ERROR}
    syslog = update['$push']['logs.$[].system_logs']
    assert 'pulsar.status.max_wait_seconds' in syslog
    assert '3600' in syslog
    assert 'cancel' in syslog


# --- Cancellation -----------------------------------------------------------------------------------------------------

def test_cancellation_stops_polling():
    pulsar = FakePulsar(RUNNING)
    checks = []

    async def canceled_on_third_check():
        checks.append(True)
        return len(checks) >= 3

    with pytest.raises(TaskCanceledError):
        _poll(pulsar, is_canceled=canceled_on_third_check)
    assert pulsar.polls == 2  # no status request once the task is canceled


def test_failing_cancel_check_does_not_fail_the_task():
    async def broken():
        raise RuntimeError('mongo down')

    status, _ = _poll(FakePulsar(RUNNING, DONE), is_canceled=broken)
    assert status == DONE


# --- Settings ---------------------------------------------------------------------------------------------------------

def _props(**status):
    return {'pulsar': {'status': status}}


def test_defaults():
    polling = load_status_polling(_props())
    assert polling == StatusPolling()
    assert polling.poll_interval == 4
    assert polling.max_poll_interval == 60
    assert polling.max_wait_seconds == 0
    assert polling.error_retry_seconds == 600


def test_settings_are_read():
    polling = load_status_polling(_props(poll_interval=1, max_poll_interval=2, max_wait_seconds=30,
                                         error_retry_seconds=5))
    assert polling == StatusPolling(poll_interval=1, max_poll_interval=2, max_wait_seconds=30, error_retry_seconds=5)


def test_repo_settings_toml_is_valid():
    from tesp_api.config.properties import properties
    load_status_polling(properties)


@pytest.mark.parametrize("status", [
    {'poll_interval': 0}, {'poll_interval': -1}, {'poll_interval': 'often'},
    {'poll_interval': 10, 'max_poll_interval': 5},
    {'max_wait_seconds': -1}, {'error_retry_seconds': -1},
])
def test_invalid_settings_are_refused(status):
    with pytest.raises(ValueError, match='pulsar.status'):
        load_status_polling(_props(**status))


def test_max_polls_is_deprecated_and_ignored():
    messages = []
    sink = logger.add(messages.append, level='WARNING', format='{message}')
    try:
        polling = load_status_polling(_props(poll_interval=4, max_polls=400))
    finally:
        logger.remove(sink)
    assert polling.max_wait_seconds == 0  # no longer a cap of 400 × 4 s
    [message] = messages
    assert 'pulsar.status.max_polls' in message
    assert 'pulsar.status.max_wait_seconds' in message


def test_no_warning_without_max_polls():
    messages = []
    sink = logger.add(messages.append, level='WARNING', format='{message}')
    try:
        load_status_polling(_props(poll_interval=4))
    finally:
        logger.remove(sink)
    assert messages == []


# --- REST and AMQP operations against a stub Pulsar -------------------------------------------------------------------

async def _with_status_stub(answers, scenario):
    """Stub Pulsar whose /jobs/{id}/status answers with (http status, body) from ``answers``, the last one repeating."""
    requests = []

    async def status(request):
        requests.append(request.match_info['job_id'])
        code, body = answers.pop(0) if len(answers) > 1 else answers[0]
        # Pulsar sends JSON as text/html
        return web.Response(status=code, text=body, content_type='text/html')

    app = web.Application()
    app.router.add_get('/jobs/{job_id}/status', status)
    server = TestServer(app)
    await server.start_server(access_log=None)
    try:
        async with aiohttp.ClientSession() as session:
            await scenario(session, str(server.make_url('')).rstrip('/'))
    finally:
        await server.close()
    return requests


FAST = StatusPolling(poll_interval=0.01, max_poll_interval=0.02, error_retry_seconds=5)
RUNNING_JSON = (200, '{"complete": "false", "status": "running"}')
DONE_JSON = (200, '{"complete": "true", "status": "complete", "returncode": 0, "job_stdout": "hi"}')


def _operations(kind, session, url, polling=FAST):
    if kind == 'rest':
        return PulsarRestOperations(session, url, polling)
    return PulsarAmqpOperations('amqp://unused', session, url, polling)


@pytest.mark.parametrize("kind", ["rest", "amqp"])
def test_operations_poll_past_the_old_limit_and_retry_5xx(kind):
    answers = [RUNNING_JSON] * 5 + [(503, 'Service Unavailable'), (500, '<html>error</html>')] + \
              [RUNNING_JSON] * 5 + [DONE_JSON]
    result = {}

    async def scenario(session, url):
        result['status'] = await _operations(kind, session, url).job_status_complete('job1', _never_canceled)

    requests = asyncio.run(_with_status_stub(answers, scenario))
    assert result['status']['job_stdout'] == 'hi'
    assert len(requests) == 13
    assert set(requests) == {'job1'}


@pytest.mark.parametrize("kind", ["rest", "amqp"])
def test_operations_do_not_retry_4xx(kind):
    async def scenario(session, url):
        with pytest.raises(PulsarLayerConnectionError):
            await _operations(kind, session, url).job_status_complete('job1', _never_canceled)

    requests = asyncio.run(_with_status_stub([(404, 'Not Found')], scenario))
    assert len(requests) == 1


@pytest.mark.parametrize("kind", ["rest", "amqp"])
def test_operations_stop_polling_when_canceled(kind):
    async def canceled():
        return True

    async def scenario(session, url):
        with pytest.raises(TaskCanceledError):
            await _operations(kind, session, url).job_status_complete('job1', canceled)

    assert asyncio.run(_with_status_stub([RUNNING_JSON], scenario)) == []


@pytest.mark.parametrize("kind", ["rest", "amqp"])
def test_operations_default_to_the_configured_polling(kind):
    from tesp_api.service.job_polling import get_status_polling
    assert _operations(kind, None, 'http://pulsar', None).status_polling == get_status_polling()
