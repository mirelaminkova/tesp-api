import os
import re
from datetime import datetime
from enum import Enum
from typing import List, Dict, Optional, Union
from pathlib import Path
from bson.objectid import ObjectId
from pydantic import BaseModel, Field
from pydantic.class_validators import root_validator

from tesp_api.utils.types import AnyUrl, FtpUrl, S3Url, HttpUrl, HttpsUrl, LocalFileUrl
from tesp_api.repository.model.py_object_id import PyObjectId
from pydantic import BaseModel, Field, root_validator
from typing import Union


class TesTaskView(str, Enum):
    MINIMAL = "MINIMAL"
    BASIC = "BASIC"
    FULL = "FULL"


class TesTaskState(str, Enum):
    UNKNOWN = "UNKNOWN"
    QUEUED = "QUEUED"
    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETE = "COMPLETE"
    EXECUTOR_ERROR = "EXECUTOR_ERROR"
    SYSTEM_ERROR = "SYSTEM_ERROR"
    CANCELED = "CANCELED"


class TesTaskIOType(str, Enum):
    FILE = "FILE"
    DIRECTORY = "DIRECTORY"


class TesTaskInput(BaseModel):
    name: str = None
    description: str = None

    # Update this field to support the new LocalFileUrl
    url: Union[S3Url, FtpUrl, HttpUrl, HttpsUrl, LocalFileUrl] = Field(
        None, description='REQUIRED, unless "content" is set. URL in long term storage, for example: '
                          ' - s3://my-object-store/file1'
                          ' - gs://my-bucket/file2'
                          ' - file:///path/to/my/file'
                          ' - /path/to/my/file',
        example="s3://my-object-store/file1")

    path: str = Field(
        ..., description="Path of the file inside the container. Must be an absolute path.",
        example="/data/file1")

    type: TesTaskIOType = TesTaskIOType.FILE
    content: str = Field(
        None, description="File content literal. Implementations should support a minimum of 128 KiB "
                          "in this field and may define their own maximum. UTF-8 encoded If content "
                          "is not empty, “url” must be ignored.")

    @root_validator(pre=False)
    def validate_content(cls, values):
        if values.get("content"):
            values["url"] = None
        return values

class TesTaskOutput(BaseModel):
    name: str = Field(None, description="User-provided name of output file")
    description: str = Field(
        None, description="Optional users provided description field, can be used for documentation.")

    url: Union[S3Url, FtpUrl, HttpUrl, HttpsUrl] = Field(..., description='URL for the file to be copied by the TES server '
                                         'after the task is complete. For Example: '
                                         ' - s3://my-object-store/file1'
                                         ' - gs://my-bucket/file2'
                                         ' - file:///path/to/my/file')

    path: str = Field(..., description="Path of the file inside the container. Must be an absolute path. May contain"
                                       " the POSIX wildcards * and ? in its last component (TES 1.1).")
    path_prefix: str = Field(None, description="Prefix removed from each file matching a path with wildcards; the"
                                               " rest is appended to url. Required for such paths, ignored otherwise.")
    type: TesTaskIOType


class TesTaskResources(BaseModel):
    cpu_cores: int = Field(None, description="Requested number of CPUs", example=4)

    preemptible: bool = Field(
        None, description="Define if the task is allowed to run on preemptible compute instances, for example, "
                          "AWS Spot. This option may have no effect when utilized on some backends that don’t "
                          "have the concept of preemptible jobs.",
        example=False)

    ram_gb: float = Field(None, description="Requested RAM required in gigabytes (GB)", example=8)
    disk_gb: float = Field(None, description="Requested disk size in gigabytes (GB)", example=40)
    zones: List[str] = Field(
        None, description="Request that the task be run in these compute zones. How this string is utilized will "
                          "be dependent on the backend system. For example, a system based on a cluster queueing "
                          "system may use this string to define priorty queue to which the job is assigned.",
        example=["us-west-1", "eu-east-2"])


class TesTaskExecutor(BaseModel):
    image: str = Field(..., example='ubuntu:20.04', description='Name of the container image. The string will be'
                                                                ' passed as the image argument to the containerization'
                                                                ' run command. Examples: - ubuntu'
                                                                ' - quay.io/aptible/ubuntu - gcr.io/my-org/my-image'
                                                                ' - myregistryhost:5000/fedora/httpd:version1.0')

    command: List[Path] = Field(..., example=['/bin/md5', '/data/file1'],
                                description='A sequence of program arguments to execute'
                                            ', where the first argument is the program to execute (i.e. argv).')

    workdir: Path = Field(None, example='/data/',
                          description='The working directory that the command will be executed in. If not'
                                      ' defined, the system will default to the directory set by the container'
                                      ' image.')
    stdin: Path = Field(None, example='/data/file1',
                        description='Path inside the container to a file which will be piped to the executor’s'
                                    ' stdin. This must be an absolute path. This mechanism could be used in'
                                    ' conjunction with the input declaration to process a data file using a tool'
                                    ' that expects STDIN. For example, to get the MD5 sum of a file by reading'
                                    ' it into the STDIN { "command" : ["/bin/md5"], "stdin" : "/data/file1" }')
    stdout: Path = Field(None, example='/tmp/stdout.log',
                         description='Path inside the container to a file where the executor’s stdout will be'
                                     ' written to. Must be an absolute path.'
                                     ' Example: { "stdout" : "/tmp/stdout.log" }')
    stderr: Path = Field(None, example='/tmp/stderr.log',
                         description='Path inside the container to a file where the executor’s stderr will be'
                                     ' written to. Must be an absolute path.'
                                     ' Example: { "stderr" : "/tmp/stderr.log" }')
    env: Dict[str, str] = Field(None, example={
        "ENV_CONFIG_PATH": "/data/config.file",
        "BLASTDB": "/data/GRC38",
        "HMMERDB": "/data/hmmer"
    }, description='Enviromental variables to set within the container. Example: { "env" : { "ENV_CONFIG_PATH" :'
                   ' "/data/config.file", "BLASTDB" : "/data/GRC38", "HMMERDB" : "/data/hmmer" } }')


class TesTaskExecutorLog(BaseModel):
    start_time: datetime = Field(None, example='2020-10-02T10:00:00-05:00',
                                 description='Time the executor started, in RFC 3339 format.')
    end_time: datetime = Field(None, example='2020-10-02T11:00:00-05:00',
                               description='Time the executor ended, in RFC 3339 format.')
    stdout: str = Field(None, description='Stdout content. This is meant for convenience. No guarantees are made about'
                                          ' the content. Implementations may chose different approaches: only the head,'
                                          ' only the tail, a URL reference only, etc. In order to capture the full'
                                          ' stdout client should set Executor.stdout to a container file path, and use'
                                          ' Task.outputs to upload that file to permanent storage.')
    stderr: str = Field(None, description='Stderr content. This is meant for convenience. No guarantees are made about'
                                          ' the content. Implementations may chose different approaches: only the head,'
                                          ' only the tail, a URL reference only, etc. In order to capture the full'
                                          ' stderr client should set Executor.stderr to a container file path, and use'
                                          ' Task.outputs to upload that file to permanent storage.')
    exit_code: int = Field(None, description='Exit code.')


class TesTaskOutputFileLog(BaseModel):
    url: Union[S3Url, FtpUrl, HttpUrl, HttpsUrl] = Field(..., example='s3://bucket/file.txt',
                        description='URL of the file in storage, e.g. s3://bucket/file.txt')
    path: Path = Field(..., description='Path of the file inside the container. Must be an absolute path.')
    size_bytes: str = Field(..., example='1024',
                            description="Size of the file in bytes. Note, this is currently coded as a string because"
                                        " official JSON doesn't support int64 numbers.")


class TesTaskLog(BaseModel):
    logs: List[TesTaskExecutorLog] = Field(..., description='Logs for each executor')
    metadata: Dict[str, str] = Field(None, example={
        "host": "worker-001",
        "slurmm_id": 123456
    }, description='Arbitrary logging metadata included by the implementation.')
    start_time: datetime = Field(None, example='2020-10-02T10:00:00-05:00',
                                 description='When the task started, in RFC 3339 format')
    end_time: datetime = Field(None, example='2020-10-02T11:00:00-05:00',
                               description='When the task ended, in RFC 3339 format.')
    outputs: List[TesTaskOutputFileLog] = Field(..., description='Information about all output files. Directory'
                                                                 ' outputs are flattened into separate items.')
    system_logs: List[str] = Field(None, description='System logs are any logs the system decides are relevant, which'
                                                     ' are not tied directly to an Executor process. Content is'
                                                     ' implementation specific: format, size, etc. System logs may be'
                                                     ' collected here to provide convenient access. For example, the'
                                                     ' system may include the name of the host where the task is'
                                                     ' executing, an error message that caused a SYSTEM_ERROR state'
                                                     ' (e.g. disk is full), etc. System logs are only included in the'
                                                     ' FULL task view.')


class TesTask(BaseModel):
    name: str = Field(None, description='User-provided task name.')
    description: str = Field(None, description='Optional user-provided description of task for documentation purposes.')

    inputs: List[TesTaskInput] = Field(
        None, description="Input files that will be used by the task. Inputs will be downloaded"
                          " and mounted into the executor container as defined by the task request document",
        example=[TesTaskInput(path='/data/input-file', url="s3://my-object-store/input-file-1", type=TesTaskIOType.FILE)])

    outputs: List[TesTaskOutput] = Field(
        None, description="Output files. Outputs will be uploaded from the executor container to long-term storage.",
        example=[TesTaskOutput(path='/data/output-file', url="s3://my-object-store/output-file-1", type=TesTaskIOType.FILE)])

    resources: TesTaskResources = Field(None, description="Resources describes the resources requested by a task.")
    executors: List[TesTaskExecutor] = Field(..., description='An array of executors to be run. Each of the executors'
                                                              ' will run one at a time sequentially. Each executor is a'
                                                              ' different command that will be run, and each can'
                                                              ' utilize a different docker image. But each of the'
                                                              ' executors will see the same mapped inputs and volumes'
                                                              ' that are declared in the parent CreateTask message.'
                                                              ' Execution stops on the first error.')
    volumes: List[Path] = Field(None, example=["/vol/A/"],
                                description='Volumes are directories which may be used to share data between'
                                            ' Executors. Volumes are initialized as empty directories by the'
                                            ' system when the task starts and are mounted at the same path in'
                                            ' each Executor. For example, given a volume defined at /vol/A,'
                                            ' executor 1 may write a file to /vol/A/exec1.out.txt, then'
                                            ' executor 2 may read from that file. (Essentially, this'
                                            ' translates to a docker run -v flag where the container path is'
                                            ' the same for each executor).')
    tags: Dict[str, str] = Field(None, example={
        "WORKFLOW_ID": "cwl-01234",
        "PROJECT_GROUP": "alice-lab"
    }, description='A key-value map of arbitrary tags. These can be used to store meta-data and annotations about a'
                   ' task. Example: { "tags" : { "WORKFLOW_ID" : "cwl-01234", "PROJECT_GROUP" : "alice-lab" } }')


class RegisteredTesTask(TesTask):
    id: PyObjectId = Field(example="job-0012345", description="Task identifier assigned by the server",
                           default_factory=PyObjectId, alias="_id")
    state: TesTaskState = Field(TesTaskState.UNKNOWN, description='Task state as defined by the server.')
    logs: List[TesTaskLog] = Field(None, description='Task logging information. Normally, this will contain only one'
                                                     ' entry, but in the case where a task fails and is retried, an'
                                                     ' entry will be appended to this list.')
    creation_time: datetime = Field(None, example='2020-10-02T10:00:00-05:00',
                                    description='Date + time the task was created, in RFC 3339 format. This is set by'
                                                ' the system, not the client.')
    author: str = Field(None, description='Subject from OAuth2 token.')

    class Config:
        orm_mode = True
        allow_mutation = False
        json_encoders = {ObjectId: str}
        allow_population_by_field_name = True
        arbitrary_types_allowed = True


def find_unsupported_s3_urls(task: TesTask) -> List[str]:
    """
    Returns the fields (e.g. ``inputs[0].url``) holding S3 URLs that are not plain ``s3://bucket/key``, plus S3
    directory inputs, which are not supported yet. Legacy ``s3://user:pass@host:port/bucket/key`` URLs are refused so
    credentials never get stored with a task. The URLs themselves are not returned, so callers can put the result
    into responses and logs.
    """
    return [
        f"{field}[{i}].url"
        for field, items in (("inputs", task.inputs), ("outputs", task.outputs))
        for i, item in enumerate(items or [])
        if isinstance(item.url, S3Url) and (
            not item.url.is_plain()
            or (field == "inputs" and item.type == TesTaskIOType.DIRECTORY)
            # A presigned POST replaces ${filename} in the key (see presign_post_policy).
            or (field == "outputs" and item.type == TesTaskIOType.DIRECTORY and "${filename}" in item.url.key))
    ]


OUTPUT_WILDCARDS = "*?"


def has_wildcards(path: str) -> bool:
    """``*`` and ``?`` are wildcards in output paths; ``[`` is not, as it is common in file names."""
    return any(c in path for c in OUTPUT_WILDCARDS)


def find_unsupported_outputs(task: TesTask) -> List[str]:
    """
    Returns the fields of outputs that cannot be uploaded: paths that are not absolute, and TES 1.1 wildcard outputs
    that tesp-api does not support (see ``unsupported_wildcard_output_field``). Without this check such an output
    would be skipped as missing. ``path_prefix`` is ignored for paths without wildcards, as TES 1.1 says.
    """
    unsupported = []
    for i, output in enumerate(task.outputs or []):
        if not output.path.startswith("/"):
            unsupported.append(f"outputs[{i}].path")
        elif has_wildcards(output.path):
            field = unsupported_wildcard_output_field(output)
            if field:
                unsupported.append(f"outputs[{i}].{field}")
    return unsupported


def unsupported_wildcard_output_field(output: TesTaskOutput) -> Optional[str]:
    """
    For an output whose path has wildcards, the field that keeps it from being uploaded, if any: wildcards are
    supported in the last path component only, for FILE outputs, with a ``path_prefix`` that the path's directory
    starts with (so each match keeps a file name after the prefix is removed). S3 URLs are not supported yet, as
    they need a presigned POST policy for the URL prefix.
    """
    if has_wildcards(os.path.dirname(output.path)):
        return "path"
    if output.type == TesTaskIOType.DIRECTORY:
        return "type"
    prefix = output.path_prefix
    if not prefix or has_wildcards(prefix) or not (os.path.dirname(output.path).rstrip("/") + "/").startswith(prefix):
        return "path_prefix"
    if isinstance(output.url, S3Url):
        return "url"
    return None


ENV_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Separate the parts of `docker run --volume=host:container[:options]` and the binds of `singularity --bind`.
MOUNT_PATH_SEPARATORS = (":", ",")


def is_valid_env_name(name: str) -> bool:
    return ENV_NAME_PATTERN.fullmatch(name) is not None


# Singularity pulls these from a remote registry. Other schemes (e.g. ``instance://``, ``oci-archive://``; matched
# case-sensitively, so ``DOCKER://`` too) are refused.
REGISTRY_IMAGE_SCHEMES = ("docker", "library", "shub", "oras")
IMAGE_SCHEME_PATTERN = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://")


def has_registry_scheme(image: str) -> bool:
    scheme = IMAGE_SCHEME_PATTERN.match(image)
    return scheme is not None and scheme.group(1) in REGISTRY_IMAGE_SCHEMES


def is_valid_image(image: str) -> bool:
    """
    Accepts Docker references and ``docker://``, ``library://``, ``shub://`` and ``oras://`` URIs. Refuses images that
    would make the container runtime use files on the host: absolute paths (Singularity runs a host directory or
    ``.sif`` file as the container, as the Pulsar user), relative paths (starting with ``.``) and other URI schemes.
    Also refuses empty images and images starting with ``-``, which would be parsed as an option of ``docker run``.
    """
    if not image or image.startswith(("-", "/", ".")):
        return False
    return IMAGE_SCHEME_PATTERN.match(image) is None or has_registry_scheme(image)


def is_valid_mount_path(path: Union[str, Path]) -> bool:
    return not any(separator in str(path) for separator in MOUNT_PATH_SEPARATORS)


def find_invalid_container_fields(task: TesTask) -> List[str]:
    """
    Returns the fields (e.g. ``executors[0].env``) holding values that cannot be passed safely to the container
    runtime: environment variable names not matching ``[A-Za-z_][A-Za-z0-9_]*``, images refused by
    ``is_valid_image`` and mounted paths containing ``:`` or ``,`` (input paths, volumes and the directories of output
    paths, which are mounted as volumes). The values themselves are not returned.
    """
    invalid = []
    for i, executor in enumerate(task.executors):
        if not all(is_valid_env_name(name) for name in executor.env or {}):
            invalid.append(f"executors[{i}].env")
        if not is_valid_image(executor.image):
            invalid.append(f"executors[{i}].image")
    invalid.extend(f"inputs[{i}].path" for i, item in enumerate(task.inputs or [])
                   if not is_valid_mount_path(item.path))
    invalid.extend(f"outputs[{i}].path" for i, item in enumerate(task.outputs or [])
                   if not is_valid_mount_path(os.path.dirname(item.path)))
    invalid.extend(f"volumes[{i}]" for i, volume in enumerate(task.volumes or [])
                   if not is_valid_mount_path(volume))
    return invalid
