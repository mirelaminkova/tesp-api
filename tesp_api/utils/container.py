import os
import re
import shlex
from urllib.parse import urlparse
from typing import Dict, List, Tuple, Optional, Union

from pymonad.maybe import Nothing, Maybe, Just

from tesp_api.repository.model.task import TesTaskExecutor, TesTaskOutput, TesTaskIOType, \
    is_valid_env_name, is_valid_image, is_valid_mount_path
from tesp_api.utils.functional import get_else_throw, maybe_of

SHELL_PATTERN = re.compile(r"[|&;<>(){}$*?\"'\\]")
SINGULARITY_ENV_PREFIXES = ("APPTAINERENV_", "SINGULARITYENV_")

# Max bytes of a redirected executor stdout/stderr file echoed back to the job's own streams,
# so it also lands in TesTaskExecutorLog.stdout/stderr (TES: "no guarantees are made about how
# much of the stream is captured").
STDIO_LOG_TAIL_BYTES = 64 * 1024
# Keeps tab, LF, CR and printable ASCII: Pulsar decodes job output as strict UTF-8, so binary data
# or a tail cut inside a multi-byte character would break its job status endpoint.
ASCII_TEXT_FILTER = r"LC_ALL=C tr -cd '\11\12\15\40-\176'"

class ContainerCommandBuilder:
    def __init__(self, container_type: str) -> None:
        self.container_type = container_type
        self._job_id: str = ""
        self._resource_cpu: Maybe[str] = Nothing
        self._resource_mem: Maybe[str] = Nothing
        self._image: Maybe[str] = Nothing
        self._workdir: Maybe[str] = Nothing
        self._envs: Dict[str, str] = {}
        self._volumes: Dict[str, str] = {}
        self._bind_mounts: Dict[str, str] = {}
        self._dirs_to_create: List[str] = []
        self._command: Maybe[str] = Nothing
        self._stdin: Maybe[str] = Nothing
        self._stdout: Maybe[str] = Nothing
        self._stderr: Maybe[str] = Nothing

    def with_job_id(self, job_id: str):
        self._job_id = job_id
        return self

    def with_resource(self, resources: dict):
        if not resources:
            return self
        self._resource_cpu = maybe_of(resources.get("cpu_cores"))
        self._resource_mem = maybe_of(resources.get("ram_gb"))
        return self

    def with_bind_mount(self, container_path: str, host_path: str):
        self._bind_mounts[container_path] = host_path
        return self

    def with_volume(self, container_path: str, volume_name: str):
        self._volumes[container_path] = volume_name
        return self

    def with_directory(self, path: str):
        self._dirs_to_create.append(path)
        return self

    def with_image(self, image: str):
        if not is_valid_image(image):
            raise ValueError(f"invalid image name: {image!r}")
        if self.container_type == "singularity":
            # If it's an absolute path, use as-is
            if os.path.isabs(image):
                self._image = Just(image)
            # If it's already got a URI scheme, use as-is
            elif image.startswith(("docker://", "library://", "shub://", "oras://")):
                self._image = Just(image)
            # Otherwise, assume Docker Hub reference
            else:
                self._image = Just(f"docker://{image}")
        else:
            self._image = Just(image)
        return self

    def with_workdir(self, workdir: str):
        self._workdir = maybe_of(workdir)
        return self

    def with_env(self, name: str, value: str):
        self._envs[name] = value
        return self

    def with_command(self, command: List[str], stdin: Maybe[str] = Nothing,
                     stdout: Maybe[str] = Nothing, stderr: Maybe[str] = Nothing):
        self._stdin = stdin
        self._stdout = stdout
        self._stderr = stderr

        if not command:
            self._command = Nothing
            return self

        # Stream redirections need a shell inside the container; the argv is passed as
        # positional parameters ("$@") so it is never interpreted by that shell.
        if self._has_stdio():
            self._command = Just(["sh", "-c", self._stdio_script(), "sh", *map(str, command)])
            return self

        # Check if already wrapped in shell
        if self._is_shell_wrapped(command):
            self._command = Just(command)
            return self

        # Wrap in shell if needed
        if self.requires_shell(command) or self.container_type == "singularity":
            shell_cmd = " ".join(shlex.quote(str(arg)) for arg in command)
            self._command = Just(["sh", "-c", shell_cmd])
        else:
            self._command = Just(list(map(str, command)))

        return self

    def _is_shell_wrapped(self, command) -> bool:
        if isinstance(command, list):
            return len(command) >= 2 and command[0] == "sh" and command[1] == "-c"
        return False

    def requires_shell(self, command: List[str]) -> bool:
        return any(
            isinstance(arg, str) and SHELL_PATTERN.search(arg)
            for arg in command
        )

    @staticmethod
    def _path(stream: Maybe[str]) -> str:
        return stream.maybe("", lambda x: x)

    def _has_stdio(self) -> bool:
        return any(self._path(stream) for stream in (self._stdin, self._stdout, self._stderr))

    def _stdio_script(self) -> str:
        stdin, stdout, stderr = (self._path(s) for s in (self._stdin, self._stdout, self._stderr))
        same_file = bool(stdout) and stdout == stderr

        redirections = [f"{op} {shlex.quote(path)}" for op, path in
                        (("<", stdin), (">", stdout), ("2>", "" if same_file else stderr)) if path]
        if same_file:
            redirections.append("2>&1")

        # sh runs as PID 1 in the container, which ignores SIGTERM unless trapped, so run the
        # command in the background and forward termination to it (e.g. on task cancel).
        script = [
            " ".join(['"$@"', *redirections]) + " & pid=$!",
            "trap 'kill -TERM $pid 2>/dev/null' TERM INT HUP",
            "wait $pid",
            "rc=$?",
            # wait returns early when a trapped signal arrives; keep waiting for the real exit code
            "while kill -0 $pid 2>/dev/null; do wait $pid; rc=$?; done",
        ]
        # Echo the tail of redirected files to the job's streams so ExecutorLog still gets them.
        if stdout:
            script.append(f"tail -c {STDIO_LOG_TAIL_BYTES} -- {shlex.quote(stdout)} 2>/dev/null"
                          f" | {ASCII_TEXT_FILTER} 2>/dev/null")
        if stderr and not same_file:
            # >&2 before 2>/dev/null: the copy goes to the original stderr, tr's own errors are dropped
            script.append(f"tail -c {STDIO_LOG_TAIL_BYTES} -- {shlex.quote(stderr)} 2>/dev/null"
                          f" | {ASCII_TEXT_FILTER} >&2 2>/dev/null")
        script.append("exit $rc")
        return "; ".join(script)

    def reset(self) -> None:
        self._resource_cpu = Nothing
        self._resource_mem = Nothing
        self._image = Nothing
        self._workdir = Nothing
        self._envs = {}
        self._volumes = {}
        self._bind_mounts = {}
        self._dirs_to_create = []
        self._command = Nothing
        self._stdin = Nothing
        self._stdout = Nothing
        self._stderr = Nothing
        return self

    def get_run_command(self) -> str:
        """
        The host command line that runs the container. Pulsar runs it through a shell, so every value is quoted
        with ``shlex.quote`` and passed in ``--option=value`` form, which a value starting with ``-`` cannot break.
        Raises ``ValueError`` for values that quoting cannot make safe (see ``tesp_api.repository.model.task``).
        """
        docker = self.container_type == "docker"

        # Common resource flags
        cpu_flag = self._resource_cpu.maybe("", lambda cpu: f"--cpus={cpu}" if docker else f"--cpu={cpu}")
        mem_flag = self._resource_mem.maybe("", lambda mem: f"--memory={mem}g" if docker else f"--memory={mem}G")

        # Environment variables
        for name in self._envs:
            if not is_valid_env_name(name):
                raise ValueError(f"invalid environment variable name: {name!r}")
        if docker:
            env_flags = [shlex.quote(f"--env={k}={v}") for k, v in self._envs.items()]
            env_prefix = []
        else:
            # `singularity exec --env` parses its value as CSV and trims quotes, so values go through the
            # environment instead. Apptainer prefers APPTAINERENV_ and logs a notice for SINGULARITYENV_ alone;
            # SingularityCE only reads SINGULARITYENV_. Apptainer accepts both when the values are equal.
            env_flags = []
            env_prefix = [f"{prefix}{k}={shlex.quote(v)}"
                          for k, v in self._envs.items() for prefix in SINGULARITY_ENV_PREFIXES]

        # Work directory
        workdir_flag = self._workdir.maybe("", lambda w:
                                           shlex.quote(f"--workdir={w}" if docker else f"--pwd={w}"))

        # Image
        image = self._image.maybe("", shlex.quote)

        # Mounts
        mount_flags = []
        mkdir_paths = []  # for singularity pre-creation

        # Handle explicit directories (Singularity only)
        if not docker:
            mkdir_paths.extend(self._dirs_to_create)

        mount_option = "--volume" if docker else "--bind"
        for container_path, host_path in [*self._bind_mounts.items(), *self._volumes.items()]:
            if not (is_valid_mount_path(container_path) and is_valid_mount_path(host_path)):
                raise ValueError(f"invalid mount path: {host_path!r}:{container_path!r}")
            mount_flags.append(shlex.quote(f"{mount_option}={host_path}:{container_path}"))
        if not docker:
            # Volumes are always directories, so we can auto-create them
            mkdir_paths.extend(self._volumes.values())
        mkdir_cmds = [f"mkdir -p -- {shlex.quote(str(path))}" for path in mkdir_paths]

        # Command
        command_str = self._command.maybe("", lambda cmd:
        " ".join(shlex.quote(arg) for arg in cmd) if isinstance(cmd, list) else cmd)

        # Build final command
        runtime = "docker run" if docker else "singularity exec"
        container_cmd = " ".join(filter(None, [
            *env_prefix, runtime, cpu_flag, mem_flag, workdir_flag, *env_flags, *mount_flags, image, command_str]))
        return " && ".join([*mkdir_cmds, container_cmd])


# Unified command functions
def stage_in_command(
    executor: TesTaskExecutor,
    resource_conf: dict,
    bind_mount: str,
    input_confs: List[dict],
    container_type: str
) -> str:
    commands = []
    for input_conf in input_confs:
        url = input_conf.get('url')
        if not url:
            continue
            
        path = input_conf['pulsar_path']
        input_type = input_conf.get('type', TesTaskIOType.FILE)
        filename = os.path.basename(path)
        
        if input_type == TesTaskIOType.DIRECTORY:
            # Recursive download
            commands.append(
                f"wget -e robots=off --mirror --no-parent --no-host-directories "
                f"--directory-prefix={shlex.quote(filename)} {shlex.quote(url)}"
            )
        else:
            # Single file download
            commands.append(f"curl -f -o {shlex.quote(filename)} {shlex.quote(url)}")
    
    # Inline (content) inputs are uploaded to Pulsar directly, so there may be nothing to download
    if not commands:
        return ""

    builder = ContainerCommandBuilder(container_type) \
        .with_image(executor.image) \
        .with_workdir(executor.workdir) \
        .with_resource(resource_conf) \
        .with_directory(bind_mount) \
        .with_command(["sh", "-c", " && ".join(commands)])

    builder.with_bind_mount(executor.workdir, bind_mount)
    
    if executor.env:
        for env_name, env_value in executor.env.items():
            builder.with_env(env_name, env_value)

    return builder.get_run_command()

def untraced(command: str) -> str:
    """
    Runs ``command`` with shell tracing off. The Pulsar job script starts with ``set -xe`` (see event_actions), so
    tracing is switched back on afterwards. ``{ set +x; } 2>/dev/null`` keeps the ``set +x`` itself out of the trace.
    """
    return f"{{ set +x; }} 2>/dev/null && {command} && set -x"

def run_command(
    executor: TesTaskExecutor,
    job_id: str,
    resource_conf: dict,
    volume_confs: List[dict],
    input_confs: List[dict],
    output_confs: List[dict],
    inputs_directory: str,
    container_type: str,
    job_directory: Optional[str] = None,
    executor_index: int = 0
) -> str:
    builder = ContainerCommandBuilder(container_type) \
        .with_job_id(job_id) \
        .with_image(executor.image) \
        .with_command(
            list(map(str, executor.command)),
            maybe_of(executor.stdin).map(str),
            maybe_of(executor.stdout).map(str),
            maybe_of(executor.stderr).map(str)
        ) \
        .with_workdir(executor.workdir) \
        .with_resource(resource_conf)

    if executor.env:
        for env_name, env_value in executor.env.items():
            builder.with_env(env_name, env_value)

    # Handle volumes and bind mounts
    for volume_conf in volume_confs:
        builder.with_volume(volume_conf['container_path'], volume_conf['volume_name'])
    
    for input_conf in input_confs:
        if input_conf.get('type') == TesTaskIOType.DIRECTORY:
            builder.with_directory(input_conf['pulsar_path'])

        builder.with_bind_mount(input_conf['container_path'], input_conf['pulsar_path'])

    if not executor.workdir and container_type == "singularity":
        builder.with_workdir("/")

    return builder.get_run_command()

def stage_out_command(
    executor: TesTaskExecutor,
    resource_conf: dict,
    output_confs: List[dict],
    volume_confs: List[dict],
    container_type: str,
    bind_mount: Optional[str] = None,
    job_directory: Optional[str] = None
) -> str:
    builder = ContainerCommandBuilder(container_type) \
        .with_image(executor.image) \
        .with_workdir(executor.workdir) \
        .with_resource(resource_conf)

    commands = []
    for output in output_confs:
        path = output['container_path']
        url = output['url']
        output_type = output.get('type', TesTaskIOType.FILE)
        
        if output_type == TesTaskIOType.DIRECTORY:
            # Recursive upload
            cmd = (
                f"find {shlex.quote(path)} -type f -exec "
                f"curl -f -X POST -F 'file=@{{}}' {shlex.quote(url)} \\;"
            )
        else:
            # Single file upload
            cmd = f"curl -f -X POST -F 'file=@{shlex.quote(path)}' {shlex.quote(url)}"
        
        commands.append(cmd)
    
    if commands:
        builder.with_command(["sh", "-c", " && ".join(commands)])

    # Mount required directories
    if container_type == "singularity" and bind_mount:
        builder.with_bind_mount(executor.workdir, bind_mount)
        builder.with_directory(bind_mount)
    
    for volume_conf in volume_confs:
        builder.with_volume(volume_conf['container_path'], volume_conf['volume_name'])

    if executor.env:
        for env_name, env_value in executor.env.items():
            builder.with_env(env_name, env_value)

    return builder.get_run_command()

def map_volumes(job_id: str, volumes: List[str], outputs: List[TesTaskOutput]):
    output_confs: List[dict] = []
    volume_confs: List[dict] = []
    existing_volume_paths = []

    # Process outputs
    for output in outputs:
        output_dirname = os.path.dirname(output.path)
        volume_name = f"vol-{job_id}-{output_dirname.replace('/', '')}"

        if output_dirname not in existing_volume_paths:
            volume_confs.append({
                'volume_name': volume_name,
                'container_path': output_dirname
            })
            existing_volume_paths.append(output_dirname)

        output_confs.append({
            'container_path': output.path,
            'url': output.url,
            'volume_name': volume_name,
            'type': output.type
        })

    for v in volumes:
        if str(v) not in existing_volume_paths:
            volume_confs.append({
                'volume_name': f"vol-{job_id}-{str(v).replace('/', '')}",
                'container_path': str(v)
            })

    return output_confs, volume_confs
