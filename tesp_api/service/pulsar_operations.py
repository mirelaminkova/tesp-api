import asyncio
import json
import aio_pika
from enum import Enum
from typing import Awaitable, Callable, Literal, Optional, Tuple
from abc import ABC, abstractmethod
from urllib.parse import quote

from pymonad.maybe import Maybe, Nothing
from bson.objectid import ObjectId
from pymonad.promise import Promise
from aiohttp import ClientSession, ClientError, ClientResponseError, TraceRequestStartParams
from loguru import logger

from tesp_api.repository.model.task import TesTaskIOType
from tesp_api.service.client.client_s3 import redact_presigned_urls
from tesp_api.service.job_polling import StatusPolling, poll_until_complete


# Pulsar returns job_stdout/job_stderr untruncated; keep the tail so task documents stay small
# (TES: "no guarantees are made about how much of the stream is captured").
MAX_JOB_STREAM_CHARS = 64 * 1024


def job_streams(status: dict) -> Tuple[str, str]:
    """Stdout and stderr of a completed Pulsar job's command line, tail only.

    Pulsar >= 0.15 reports them as `job_stdout`/`job_stderr`; its `stdout`/`stderr` are Galaxy's
    tool_stdout/tool_stderr metadata files, which TESP never writes, so they are only a fallback
    for older Pulsar that filled them from the command itself.
    """
    def tail(job_key: str, legacy_key: str) -> str:
        return (status.get(job_key) or status.get(legacy_key) or '')[-MAX_JOB_STREAM_CHARS:]

    return tail('job_stdout', 'stdout'), tail('job_stderr', 'stderr')


# aiohttp tracing feature allows to log each request
async def log_pulsar_request(session, context, params: TraceRequestStartParams):
    # Method and path only: the query of /jobs/{id}/submit is the whole job command line, presigned S3 URLs included.
    logger.debug(f'Sending request to pulsar <{params.method} {params.url.with_query(None)}>')


def _error_message(error: Exception) -> str:
    # aiohttp's ClientResponseError names the request URL, and that may hold the job command line. The type is named
    # because the wrapped error is hidden from tracebacks, and some (asyncio.TimeoutError) have no message.
    return f'{type(error).__name__}: {redact_presigned_urls(str(error))}'


class DataType(str, Enum):
    INPUT = "input",
    OUTPUT = "output"


class PulsarLayerConnectionError(Exception):
    def __init__(self, error: Exception):
        self.message = f'Pulsar connection error occurred [msg: {_error_message(error)}]'
        # HTTP status of an error answer from Pulsar, None when there was no answer (connection error, timeout)
        self.http_status: Optional[int] = error.status if isinstance(error, ClientResponseError) else None
        super().__init__(self.message)
        # Logged tracebacks would print the unredacted wrapped error as the context ("During handling of...").
        self.__suppress_context__ = True

    @property
    def transient(self) -> bool:
        """Whether retrying may help: no answer, an overloaded Pulsar (429) or a server error (5xx)."""
        return self.http_status is None or self.http_status == 429 or self.http_status >= 500

    def __repr__(self):
        return f'PulsarLayerConnectionError [message: {self.message}]'


def is_transient_pulsar_error(error: Exception) -> bool:
    return isinstance(error, PulsarLayerConnectionError) and error.transient


class PulsarOperationsError(Exception):
    def __init__(self, error: Exception):
        self.message = f'Pulsar operations error occurred [msg: {_error_message(error)}]'
        super().__init__(self.message)
        # Logged tracebacks would print the unredacted wrapped error as the context ("During handling of...").
        self.__suppress_context__ = True

    def __repr__(self):
        return f'PulsarOperationsError [message: {self.message}]'


class PulsarOperations(ABC):
    status_polling: StatusPolling

    @abstractmethod
    async def _pulsar_request(self, path: str, method: Literal['GET', 'POST', 'PUT', 'DELETE'],
                              response_type: Literal['JSON', 'BYTES'], params=None, data=None,
                              raise_for_status: bool = False):
        """A request to Pulsar's REST API; ``raise_for_status`` turns 4xx/5xx answers into errors."""

    @abstractmethod
    def erase_job(self, task_id: ObjectId): pass

    @abstractmethod
    def setup_job(self, job_id: ObjectId): pass

    @abstractmethod
    def run_job(self, job_id: ObjectId, run_command: str, submit_params: Optional[dict] = None):
        """Submits the job; ``submit_params`` (e.g. ``native_specification``) go to Pulsar's job manager."""

    async def _job_status(self, job_id: str) -> dict:
        try:
            status = await self._pulsar_request(path=f'/jobs/{job_id}/status', method='GET', response_type='JSON',
                                                raise_for_status=True)
        except PulsarLayerConnectionError as error:
            if error.transient:
                raise
            raise PulsarOperationsError(error)  # Pulsar refused the request (4xx); retrying will not help
        if not isinstance(status, dict) or 'complete' not in status:
            # Else polling would wait forever for a 'complete' that never comes.
            raise PulsarOperationsError(ValueError(f'unexpected job status answer of type {type(status).__name__}'))
        return status

    async def job_status_complete(self, job_id: str, is_canceled: Callable[[], Awaitable[bool]],
                                  on_started: Optional[Callable[[], Awaitable[None]]] = None) -> dict:
        """Waits for the job to finish and returns its Pulsar status (see ``poll_until_complete``); ``on_started`` is
        awaited once the job has left the batch system's queue.

        Both implementations read the status from Pulsar's REST API."""
        return await poll_until_complete(lambda: self._job_status(job_id), is_canceled, self.status_polling,
                                         is_transient=is_transient_pulsar_error, job_id=job_id,
                                         on_started=on_started)

    @abstractmethod
    def upload(self, job_id: ObjectId, io_type: TesTaskIOType, file_path: str, file_content: Maybe[str]): pass

    @abstractmethod
    def download_output(self, job_id: ObjectId, file_name: str): pass

class PulsarRestOperations(PulsarOperations):

    def __init__(self, pulsar_client: ClientSession, base_url: str, status_polling: StatusPolling):
        self.pulsar_client = pulsar_client
        self.base_url = base_url
        self.status_polling = status_polling

    @staticmethod
    def _reraise_custom(error: Exception):
        match error:
            case PulsarLayerConnectionError() as client_error: raise client_error
            case _ as any_error: raise PulsarOperationsError(any_error)

    async def _pulsar_request(self, path: str, method: Literal['GET', 'POST', 'PUT', 'DELETE'],
                              response_type: Literal['JSON', 'BYTES'], params=None, data=None,
                              raise_for_status: bool = False):
        try:
            async with self.pulsar_client.request(
                    url=f'{self.base_url}{path}', method=method, params=params, data=data) as response:
                if raise_for_status:
                    response.raise_for_status()
                match response_type:
                    case 'JSON': return await response.json(content_type='text/html')
                    case 'BYTES': return await response.read()
                    case _ as value: raise ValueError(f'Got unexpected value [{value}] for response_type parameter')
        except (ClientError, asyncio.TimeoutError) as err:
            raise PulsarLayerConnectionError(err)

    def setup_job(self, job_id: ObjectId) -> Promise:
        return Promise(lambda resolve, reject: resolve(None))\
            .then(lambda nothing: self._pulsar_request(
                path="/jobs", method='POST',
                response_type='JSON', params={'job_id': str(job_id)}
            )).catch(self._reraise_custom)

    def upload(self, job_id: ObjectId, io_type: TesTaskIOType, file_path: str, file_content: Maybe[str] = Nothing):
        return Promise(lambda resolve, reject: resolve({'type': io_type.value, 'name': file_path}))\
            .then(lambda query_params: self._pulsar_request(
                path=f'/jobs/{str(job_id)}/files', method='POST', response_type='JSON',
                params=query_params, data=file_content.maybe("", lambda x: x)
            )).map(lambda json_result: json_result['path'])

    def run_job(self, job_id: ObjectId, run_command: str, submit_params: Optional[dict] = None):
        params = {'command_line': run_command}
        if submit_params:
            params['params'] = json.dumps(submit_params)
        return Promise(lambda resolve, reject: resolve(None))\
            .then(lambda nothing: self._pulsar_request(
                path=f'/jobs/{str(job_id)}/submit', method='POST', response_type='BYTES',
                params=params)) \
            .catch(self._reraise_custom)

    def download_output(self, job_id: ObjectId, file_name: str):
        return Promise(lambda resolve, reject: resolve(None))\
            .then(lambda nothing: self._pulsar_request(
                path=f'/jobs/{str(job_id)}/files', method='GET', response_type='BYTES',
                params={'name': file_name}
            )).catch(self._reraise_custom)

    def erase_job(self, job_id: ObjectId):
        return Promise(lambda resolve, reject: resolve(None)) \
           .then(lambda nothing: self._pulsar_request(
               path=f'/jobs/{str(job_id)}/cancel',
               method='PUT', response_type='BYTES'
           )).catch(lambda error: None)\
           .then(lambda nothing: self._pulsar_request(
               path=f'/jobs/{str(job_id)}',
               method='DELETE', response_type='BYTES'
           )).catch(self._reraise_custom)


class PulsarAmqpOperations(PulsarOperations):
    def __init__(self, amqp_url: str, pulsar_client: ClientSession, base_url: str,
                 status_polling: StatusPolling):
        self.amqp_url = amqp_url
        self.pulsar_client = pulsar_client
        self.base_url = base_url
        self.status_polling = status_polling
        self.connection: aio_pika.RobustConnection | None = None
        self.channel: aio_pika.Channel | None = None

    async def _connect(self):
        if self.connection is None or self.connection.is_closed:
            self.connection = await aio_pika.connect_robust(self.amqp_url)
            self.channel = await self.connection.channel()

    async def _send_message(self, message: dict, routing_key: str = "pulsar.queue"):
        await self._connect()
        await self.channel.default_exchange.publish(
            aio_pika.Message(body=json.dumps(message).encode()),
            routing_key=routing_key
        )

    @staticmethod
    def _reraise_custom(error: Exception):
        match error:
            case PulsarLayerConnectionError() as client_error: raise client_error
            case _ as any_error: raise PulsarOperationsError(any_error)

    async def _pulsar_request(self, path: str, method: Literal['GET', 'POST', 'PUT', 'DELETE'],
                              response_type: Literal['JSON', 'BYTES'], params=None, data=None,
                              raise_for_status: bool = False):
        try:
            async with self.pulsar_client.request(
                    url=f'{self.base_url}{path}', method=method, params=params, data=data) as response:
                if raise_for_status:
                    response.raise_for_status()
                match response_type:
                    case 'JSON': return await response.json(content_type='text/html')
                    case 'BYTES': return await response.read()
                    case _ as value: raise ValueError(f'Unexpected response_type: {value}')
        except (ClientError, asyncio.TimeoutError) as err:
            raise PulsarLayerConnectionError(err)

    def setup_job(self, job_id: str):
        return Promise(lambda resolve, reject: resolve(None))\
            .then(lambda _: self._send_message({
                "type": "job",
                "job_id": str(job_id)
            })).catch(lambda e: PulsarOperationsError(e))

    def run_job(self, job_id: ObjectId, run_command: str, submit_params: Optional[dict] = None):
        message = {"type": "submit", "job_id": str(job_id), "command_line": run_command}
        if submit_params:
            message["submit_params"] = submit_params
        return Promise(lambda resolve, reject: resolve(None))\
            .then(lambda _: self._send_message(message)).catch(lambda e: PulsarOperationsError(e))

    def erase_job(self, task_id: ObjectId):
        return Promise(lambda resolve, reject: resolve(None))\
            .then(lambda _: self._send_message({
                "type": "cancel",
                "job_id": str(task_id)
            })).catch(lambda e: PulsarOperationsError(e))

    def upload(self, job_id: ObjectId, io_type: TesTaskIOType, file_path: str, file_content: Maybe[str] = Nothing):
        return Promise(lambda resolve, reject: resolve({'type': io_type.value, 'name': file_path}))\
            .then(lambda query_params: self._pulsar_request(
                path=f'/jobs/{str(job_id)}/files', method='POST', response_type='JSON',
                params=query_params, data=file_content.maybe("", lambda x: x)
            )).map(lambda json_result: json_result['path'])

    def download_output(self, job_id: ObjectId, file_name: str):
        return Promise(lambda resolve, reject: resolve(None))\
            .then(lambda _: self._pulsar_request(
                path=f'/jobs/{str(job_id)}/files', method='GET', response_type='BYTES',
                params={'name': file_name}
            )).catch(self._reraise_custom)
