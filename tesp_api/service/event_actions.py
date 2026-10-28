import os
import datetime
from typing import Awaitable, List, Optional
from pathlib import Path
import asyncio

from loguru import logger

from pymonad.maybe import Just, Nothing
from bson.objectid import ObjectId
from pymonad.promise import Promise, _Promise

from tesp_api.utils.container import stage_in_command, run_command, stage_out_command, map_volumes
from tesp_api.utils.job_script import job_script
from tesp_api.service.pulsar_service import pulsar_service
from tesp_api.service.resources import get_resource_config
from tesp_api.service.event_dispatcher import dispatch_event
from tesp_api.utils.functional import get_else_throw, maybe_of
from tesp_api.service.event_handler import Event, local_handler
from tesp_api.repository.task_repository import task_repository
from tesp_api.service.file_transfer_service import file_transfer_service
from tesp_api.service.client.client_s3 import presign_s3_inputs, presign_s3_outputs, redact_presigned_urls
from tesp_api.service.error import pulsar_event_handle_error, pulsar_cancel_task_promise, TaskNotFoundError, \
    TaskExecutorError
from tesp_api.service.job_polling import TaskCanceledError
from tesp_api.service import task_recovery
from tesp_api.service.pulsar_operations import PulsarRestOperations, PulsarAmqpOperations, DataType, job_streams
from tesp_api.repository.model.task import (
    TesTaskState,
    TesTaskExecutor,
    TesTaskResources,
    TesTaskInput,
    TesTaskOutput,
    TesTaskIOType
)
from tesp_api.repository.task_repository_utils import append_task_executor_logs, update_last_task_log_time

CONTAINER_TYPE = os.getenv("CONTAINER_TYPE", "docker")


@local_handler.register(event_name="queued_task")
def handle_queued_task(event: Event) -> None:
    """
    Dispatches the task to a REST or AMQP specific handler based on Pulsar operations type.
    """
    event_name, payload = event
    logger.info(f"Queued task: {payload.get('task_id')}")
    match pulsar_service.get_operations():
        case PulsarRestOperations() as pulsar_rest_operations:
            dispatch_event('queued_task_rest', {**payload, 'pulsar_operations': pulsar_rest_operations})
        case PulsarAmqpOperations() as pulsar_amqp_operations:
            dispatch_event('queued_task_amqp', {**payload, 'pulsar_operations': pulsar_amqp_operations})


@local_handler.register(event_name="queued_task_rest")
async def handle_queued_task_rest(event: Event):
    """
    Sets up the job in Pulsar via REST operations and dispatches an 'initialize_task' event.
    """
    event_name, payload = event
    task_id: ObjectId = payload['task_id']
    pulsar_operations: PulsarRestOperations = payload['pulsar_operations']

    logger.debug(f"Queued task rest: {task_id}")
    await task_recovery.take(task_id)

    await Promise(lambda resolve, reject: resolve(None)) \
        .then(lambda nothing: pulsar_operations.setup_job(task_id)) \
        .map(lambda setup_job_result: dispatch_event('initialize_task', {**payload, 'task_config': setup_job_result})) \
        .catch(lambda error: pulsar_event_handle_error(error, task_id, event_name, pulsar_operations)) \
        .then(lambda x: x)  # Invokes promise, potentially from error handler


@local_handler.register(event_name="queued_task_amqp")
async def handle_queued_task_amqp(event: Event):
    """
    Sets up the job in Pulsar via AMQP operations and dispatches an 'initialize_task' event.
    """
    event_name, payload = event
    task_id: ObjectId = payload['task_id']
    pulsar_operations: PulsarAmqpOperations = payload['pulsar_operations']

    logger.debug(f"Queued task AMQP: {task_id}")

    try:
        await task_recovery.take(task_id)
        # Setup job via AMQP
        setup_job_result = await pulsar_operations.setup_job(task_id)

        # Dispatch initialize event
        await dispatch_event('initialize_task', {
            **payload,
            'task_config': setup_job_result
        })
    except Exception as error:
        await pulsar_event_handle_error(error, task_id, event_name, pulsar_operations)


@local_handler.register(event_name="initialize_task")
async def handle_initializing_task(event: Event) -> None:
    """
    Updates task state to INITIALIZING, prepares data/volumes/inputs/outputs,
    and dispatches a 'run_task' event.
    """
    event_name, payload = event
    task_id: ObjectId = payload['task_id']
    pulsar_operations: PulsarRestOperations = payload['pulsar_operations']

    async def setup_data(job_id: ObjectId,
                         resources: TesTaskResources,
                         volumes: List[str],
                         inputs: List[TesTaskInput],
                         outputs: List[TesTaskOutput]):
        resource_conf: dict
        volume_confs: List[dict] = []
        input_confs: List[dict] = []
        output_confs: List[dict] = []

        resource_conf = ({
            'cpu_cores': resources.cpu_cores if resources else None,
            'ram_gb': resources.ram_gb if resources else None
        })

        output_confs, volume_confs = map_volumes(str(job_id), volumes, outputs)

        for i, input_item in enumerate(inputs):
            if input_item.type == TesTaskIOType.DIRECTORY:
                pulsar_path = payload['task_config']['inputs_directory'] + f'/input_dir_{i}'
            elif input_item.content is not None and input_item.url is None:
                pulsar_path = await pulsar_operations.upload(
                    job_id, DataType.INPUT,
                    file_content=Just(input_item.content),
                    file_path=f'input_file_{i}'
                )
            else:
                pulsar_path = payload['task_config']['inputs_directory'] + f'/input_file_{i}'

            input_confs.append({
                'container_path': input_item.path,
                'pulsar_path': pulsar_path,
                'url': input_item.url,
                'type': input_item.type
            })

        return resource_conf, volume_confs, input_confs, output_confs

    logger.info(f"Initializing task: {task_id}")

    await Promise(lambda resolve, reject: resolve(None)) \
        .then(lambda nothing: task_repository.update_task_state(
        task_id,
        TesTaskState.QUEUED,
        TesTaskState.INITIALIZING
    )).map(lambda updated_task: get_else_throw(
        updated_task, TaskNotFoundError(task_id, Just(TesTaskState.QUEUED))
    )).then(lambda updated_task: setup_data(
        task_id,
        maybe_of(updated_task.resources).maybe(None, lambda x: x),
        maybe_of(updated_task.volumes).maybe([], lambda x: x),
        maybe_of(updated_task.inputs).maybe([], lambda x: x),
        maybe_of(updated_task.outputs).maybe([], lambda x: x)
    )).map(lambda res_input_output_confs: dispatch_event('run_task', {
        **payload,
        'resource_conf': res_input_output_confs[0],
        'volume_confs': res_input_output_confs[1],
        'input_confs': res_input_output_confs[2],
        'output_confs': res_input_output_confs[3]
    })).catch(lambda error: pulsar_event_handle_error(error, task_id, event_name, pulsar_operations)) \
        .then(lambda x: x)


@local_handler.register(event_name="run_task")
async def handle_run_task(event: Event) -> None:
    """
    Updates task state to RUNNING, prepares and submits the job to Pulsar, then waits for it to finish (see
    ``_wait_and_finish``).
    """
    event_name, payload = event
    task_id: ObjectId = payload['task_id']
    author: str = payload['author']
    resource_conf: dict = payload['resource_conf']
    volume_confs: List[dict] = payload['volume_confs']
    input_confs: List[dict] = payload['input_confs']
    output_confs: List[dict] = payload['output_confs']
    pulsar_operations: PulsarRestOperations = payload['pulsar_operations']

    async def run():
        nonlocal resource_conf
        logger.info(f"Running task: {task_id}")
        # Set task state to RUNNING
        task_monad_init = await task_repository.update_task_state(
            task_id,
            TesTaskState.INITIALIZING,
            TesTaskState.RUNNING
        )
        task = get_else_throw(task_monad_init, TaskNotFoundError(task_id, Just(TesTaskState.INITIALIZING)))

        # Early check for cancellation
        current_task_after_init_monad = await task_repository.get_task(maybe_of(author), {'_id': task_id})
        current_task_after_init = get_else_throw(current_task_after_init_monad, TaskNotFoundError(task_id))
        if current_task_after_init.state == TesTaskState.CANCELED:
            logger.warning(f"Task {task_id} found CANCELED shortly after RUNNING state update. Aborting handler.")
            return

        await update_last_task_log_time(
            task_id,
            author,
            TesTaskState.RUNNING,
            start_time=Just(datetime.datetime.now(datetime.timezone.utc))
        )

        # TES resources go to the batch system (native specification) and, per container.resource_limits, onto the
        # container command lines.
        resource_config = get_resource_config()
        submit_params = resource_config.submit_params(resource_conf)
        resource_conf = resource_config.container_resource_conf(resource_conf, CONTAINER_TYPE)

        stage_exec = TesTaskExecutor(image="willdockerhub/curl-wget:latest", command=[], workdir=Path("/downloads"))

        # Stage-in command
        stage_in_cmd = ""
        stage_in_mount = ""
        if input_confs:
            stage_in_mount = payload['task_config']['inputs_directory']
            stage_in_confs = presign_s3_inputs(input_confs)
            stage_in_cmd = stage_in_command(stage_exec, resource_conf, stage_in_mount, stage_in_confs, CONTAINER_TYPE)

        # Main execution commands
        container_cmds = []
        for i, executor in enumerate(task.executors):
            run_cmd = run_command(
                executor=executor, job_id=str(task_id), resource_conf=resource_conf,
                volume_confs=volume_confs, input_confs=input_confs, output_confs=output_confs,
                inputs_directory=stage_in_mount, container_type=CONTAINER_TYPE,
                job_directory=payload['task_config'].get('job_directory') if CONTAINER_TYPE == "singularity" else None,
                executor_index=i
            )
            container_cmds.append(run_cmd)

        # Stage-out command
        stage_out_cmd = ""
        if output_confs:
            stage_out_confs = presign_s3_outputs(output_confs)
            stage_out_cmd = stage_out_command(
                stage_exec, resource_conf, stage_out_confs, volume_confs,
                container_type=CONTAINER_TYPE,
                bind_mount=payload['task_config'].get('inputs_directory') if CONTAINER_TYPE == "singularity" else None,
                job_directory=payload['task_config'].get('job_directory') if CONTAINER_TYPE == "singularity" else None
            )

        # Single job script for Pulsar (untraced, see job_script)
        run_command_str = job_script(stage_in_cmd, *container_cmds, stage_out_cmd)

        command_start_time = datetime.datetime.now(datetime.timezone.utc)

        if run_command_str is None:
            logger.warning(f"Task {task_id} has no commands to run. Treating as successful no-op.")
            await _finish(task_id, author, pulsar_operations, event_name, command_start_time,
                          {'stdout': '', 'stderr': 'No commands to run.', 'returncode': 0})
            return

        logger.debug(f"Submitting job to Pulsar for task {task_id}: {redact_presigned_urls(run_command_str)}")
        await pulsar_operations.run_job(task_id, run_command_str, submit_params)
        # From here on another tesp-api process can resume the wait if this one stops (see task_recovery).
        await task_recovery.mark_submitted(task_id)
        await _wait_and_finish(task_id, author, pulsar_operations, event_name, command_start_time)

    await _guarded(run(), task_id, author, pulsar_operations, event_name)


@local_handler.register(event_name="resume_task")
async def handle_resume_task(event: Event) -> None:
    """
    Waits for the Pulsar job of a RUNNING task that a stopped tesp-api process submitted (see ``task_recovery``).
    """
    event_name, payload = event
    task_id: ObjectId = payload['task_id']
    author: Optional[str] = payload['author']
    pulsar_operations: PulsarRestOperations = payload['pulsar_operations']

    async def run():
        task = get_else_throw(await task_repository.get_task(maybe_of(author), {'_id': task_id}),
                              TaskNotFoundError(task_id))
        # The executor log starts when the task started running, before the restart.
        start_time = (task.logs[-1].start_time if task.logs else None) or datetime.datetime.now(datetime.timezone.utc)
        await _wait_and_finish(task_id, author, pulsar_operations, event_name, start_time)

    await _guarded(run(), task_id, author, pulsar_operations, event_name)


async def _wait_and_finish(task_id: ObjectId, author: Optional[str], pulsar_operations: PulsarRestOperations,
                           event_name: str, command_start_time: datetime.datetime) -> None:
    """Waits for the task's submitted Pulsar job to finish and records its outcome (see ``_finish``)."""
    # POST /tasks/{id}:cancel only marks the task; polling notices it and ends the Pulsar job (see _guarded).
    async def is_canceled() -> bool:
        task_monad = await task_repository.get_task(maybe_of(author), {'_id': task_id})
        return task_monad.is_just() and task_monad.value.state == TesTaskState.CANCELED

    command_status = await pulsar_operations.job_status_complete(str(task_id), is_canceled)
    await _finish(task_id, author, pulsar_operations, event_name, command_start_time, command_status)


async def _finish(task_id: ObjectId, author: Optional[str], pulsar_operations: PulsarRestOperations, event_name: str,
                  command_start_time: datetime.datetime, command_status: dict) -> None:
    """Stores the executor log of the finished job and sets the task COMPLETE or EXECUTOR_ERROR."""
    command_end_time = datetime.datetime.now(datetime.timezone.utc)
    await append_task_executor_logs(
        task_id, author, TesTaskState.RUNNING,
        command_start_time, command_end_time,
        *job_streams(command_status),
        command_status.get('returncode', -1)
    )

    current_task_monad = await task_repository.get_task(maybe_of(author), {'_id': task_id})
    current_task_obj = get_else_throw(current_task_monad, TaskNotFoundError(task_id))

    if current_task_obj.state == TesTaskState.CANCELED:
        logger.warning(f"Task {task_id} found CANCELED after job completion polling. Aborting state changes.")
        await pulsar_cancel_task_promise(str(task_id), pulsar_operations)  # erase the finished job's files
        return

    if command_status.get('returncode', -1) != 0:
        logger.error(
            f"Task {task_id} executor error (return code: {command_status.get('returncode', -1)}). Setting state to EXECUTOR_ERROR.")
        await task_repository.update_task_state(task_id, TesTaskState.RUNNING, TesTaskState.EXECUTOR_ERROR)
        await pulsar_operations.erase_job(task_id)
        return

    logger.info(f"Task {task_id} completed successfully. Setting state to COMPLETE.")
    await Promise(lambda resolve, reject: resolve(None)) \
        .then(lambda ignored: task_repository.update_task_state(
        task_id, TesTaskState.RUNNING, TesTaskState.COMPLETE
    )) \
        .map(lambda task_after_complete_update: get_else_throw(
        task_after_complete_update, TaskNotFoundError(task_id, Just(TesTaskState.RUNNING))
    )) \
        .then(lambda ignored: pulsar_operations.erase_job(task_id)) \
        .catch(lambda error: pulsar_event_handle_error(error, task_id, event_name, pulsar_operations)) \
        .then(lambda x: x)


async def _guarded(work: Awaitable[None], task_id: ObjectId, author: Optional[str],
                   pulsar_operations: PulsarRestOperations, event_name: str) -> None:
    """
    Runs a task's ``run_task``/``resume_task`` work, handles its cancellation and errors, and drops the task's lease
    once it reached a final state.
    """
    try:
        await work

    except TaskCanceledError:
        logger.info(f"Task {task_id} was canceled while its Pulsar job ran. Stopped polling, canceling the job.")
        await pulsar_cancel_task_promise(str(task_id), pulsar_operations)

    except asyncio.CancelledError:
        # tesp-api is shutting down, not the client canceling. The task keeps its state and its Pulsar job goes on;
        # another tesp-api process resumes waiting for it (see task_recovery).
        logger.info(f"Stopped waiting for task {task_id}: tesp-api is shutting down. Its Pulsar job goes on.")
        raise

    except Exception as error:
        logger.error(f"Exception in {event_name} for task {task_id}: {type(error).__name__} - "
                     f"{redact_presigned_urls(str(error))}")

        task_state_after_error_monad = await task_repository.get_task(maybe_of(author), {'_id': task_id})
        if task_state_after_error_monad.is_just() and task_state_after_error_monad.value.state == TesTaskState.CANCELED:
            logger.info(
                f"Task {task_id} is already CANCELED. Exception '{type(error).__name__}' likely due to this. No further error processing by handler.")
        else:
            logger.debug(f"Task {task_id} not CANCELED; proceeding with pulsar_event_handle_error for '{type(error).__name__}'.")
            error_handler_result = pulsar_event_handle_error(error, task_id, event_name, pulsar_operations)
            if asyncio.iscoroutine(error_handler_result) or isinstance(error_handler_result, _Promise):
                await error_handler_result

    try:
        await task_recovery.release(task_id)
    except Exception as error:
        logger.warning(f"Could not release the lease of task {task_id} [{type(error).__name__}: {error}]")
