import asyncio
import time
from dataclasses import dataclass, fields
from functools import lru_cache
from typing import Awaitable, Callable

from loguru import logger

from tesp_api.config.properties import properties

# Each wait is this much longer than the one before, up to pulsar.status.max_poll_interval.
POLL_INTERVAL_GROWTH = 1.25


class TaskCanceledError(Exception):
    """The TES task was canceled while its Pulsar job ran; polling stopped."""


class PulsarJobTimeoutError(Exception):
    def __init__(self, max_wait_seconds: float):
        self.max_wait_seconds = max_wait_seconds
        super().__init__(f'Pulsar job did not finish within pulsar.status.max_wait_seconds ({max_wait_seconds:g} s)')


@dataclass(frozen=True)
class StatusPolling:
    """
    How tesp-api waits for a Pulsar job (``pulsar.status.*`` in settings.toml).

    The status is first requested ``poll_interval`` seconds after submission; each later wait grows by
    ``POLL_INTERVAL_GROWTH`` up to ``max_poll_interval``. Polling goes on until the job is complete, unless
    ``max_wait_seconds`` (0 = no limit) runs out first. Connection errors and 5xx/429 answers from Pulsar are retried
    until they have lasted ``error_retry_seconds`` without a good answer in between (0 = not retried).
    """
    poll_interval: float = 4
    max_poll_interval: float = 60
    max_wait_seconds: float = 0
    error_retry_seconds: float = 600

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f'pulsar.status.{field.name} must be a number, got {value!r}')
        if self.poll_interval <= 0:
            raise ValueError(f'pulsar.status.poll_interval must be positive, got {self.poll_interval!r}')
        if self.max_poll_interval < self.poll_interval:
            raise ValueError(f'pulsar.status.max_poll_interval ({self.max_poll_interval!r}) must not be less than '
                             f'pulsar.status.poll_interval ({self.poll_interval!r})')
        for name in ('max_wait_seconds', 'error_retry_seconds'):
            if getattr(self, name) < 0:
                raise ValueError(f'pulsar.status.{name} must not be negative, got {getattr(self, name)!r}')


def load_status_polling(props=None) -> StatusPolling:
    props = props if props is not None else properties
    status = (props.get('pulsar') or {}).get('status') or {}
    if status.get('max_polls') is not None:
        logger.warning('pulsar.status.max_polls is deprecated and ignored: tesp-api now waits until the Pulsar job '
                       'finishes. Set pulsar.status.max_wait_seconds to limit how long a task may run.')
    return StatusPolling(**{field.name: status[field.name] for field in fields(StatusPolling)
                            if status.get(field.name) is not None})


@lru_cache(maxsize=1)
def get_status_polling() -> StatusPolling:
    """The server's status polling settings, read once."""
    return load_status_polling()


async def _canceled(is_canceled: Callable[[], Awaitable[bool]]) -> bool:
    try:
        return await is_canceled()
    except Exception as error:
        # Keep the job: a failed check (e.g. a database hiccup) is no reason to give up on a running task.
        logger.warning(f'Could not check whether the task was canceled [{type(error).__name__}: {error}]')
        return False


async def poll_until_complete(fetch_status: Callable[[], Awaitable[dict]],
                              is_canceled: Callable[[], Awaitable[bool]],
                              polling: StatusPolling,
                              is_transient: Callable[[Exception], bool] = lambda error: False,
                              job_id: str = '', sleep=asyncio.sleep, clock=time.monotonic) -> dict:
    """
    Requests the job status until Pulsar reports the job complete and returns that status.

    Raises ``TaskCanceledError`` once ``is_canceled()`` says so (checked before each request),
    ``PulsarJobTimeoutError`` when ``polling.max_wait_seconds`` is set and has passed, and the error of
    ``fetch_status()`` when it is not transient or transient errors have lasted ``polling.error_retry_seconds``.
    """
    started = clock()
    interval = polling.poll_interval
    failing_since = None
    while True:
        await sleep(interval)
        interval = min(interval * POLL_INTERVAL_GROWTH, polling.max_poll_interval)
        if await _canceled(is_canceled):
            raise TaskCanceledError()
        try:
            status = await fetch_status()
        except Exception as error:
            if not is_transient(error):
                raise
            if failing_since is None:
                failing_since = clock()
            if clock() - failing_since >= polling.error_retry_seconds:
                raise
            logger.warning(f'Pulsar status request for job {job_id} failed, retrying in {interval:g} s [{error}]')
        else:
            failing_since = None
            if status.get('complete') == 'true':
                return status
        if polling.max_wait_seconds and clock() - started >= polling.max_wait_seconds:
            raise PulsarJobTimeoutError(polling.max_wait_seconds)
