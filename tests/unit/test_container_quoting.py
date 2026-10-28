"""Task-controlled values must reach the container runtime intact and never be interpreted by the host shell.

Pulsar runs the command line from `ContainerCommandBuilder.get_run_command()` (and the stage-in/stage-out
builders) through a shell on the host. Each test feeds hostile values into the builder and checks two things:

* `shlex.split` of the command line returns each value intact, as one argument (or the expected part of a
  `NAME=value` / `host:container` argument);
* running the command line with `sh`, with stub `docker`/`singularity`/`mkdir` binaries that record their
  argv and environment, expands nothing: the recorded arguments equal the originals and no marker file appears.
"""
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from tesp_api.repository.model.task import TesTaskExecutor, TesTaskIOType
from tesp_api.utils.container import MAX_REDIRECTS, ContainerCommandBuilder, run_command, stage_in_command, stage_out_command, \
    stage_out_script

CONTAINER_TYPES = ["docker", "singularity"]
MARKER = f"/tmp/tesp-api-quoting-test-pwned-{os.getpid()}"

HOSTILE = [
    f"$(touch {MARKER})",
    f"`touch {MARKER}`",
    f'"; touch {MARKER}; echo "',
    f"'; touch {MARKER}; echo '",
    f"x; touch {MARKER}",
    f"x && touch {MARKER}",
    f"x | touch {MARKER}",
    "$HOME",
    "${HOME}",
    "two words",
    f"line1\ntouch {MARKER}",
    "tab\tseparated",
    "glob*?[a]",
    "back\\slash",
    "--privileged",
    "",
]
HOSTILE_PATHS = [f"/data/{value}" for value in HOSTILE]
HOSTILE_IDS = [repr(value)[:30] for value in HOSTILE]

STUB = f"""#!{sys.executable}
import json, os, sys
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps({{"argv": [os.path.basename(sys.argv[0]), *sys.argv[1:]],
                          "env": {{k: v for k, v in os.environ.items() if k.startswith(("APPTAINERENV_", "SINGULARITYENV_"))}}}}) + "\\n")
"""


@pytest.fixture(autouse=True)
def no_marker():
    if os.path.exists(MARKER):
        os.remove(MARKER)
    yield
    assert not os.path.exists(MARKER), "a task value was executed by the host shell"


def _run_with_stubs(command: str, tmp_path: Path):
    """Run `command` with sh, with the container runtimes and mkdir replaced by recording stubs."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("docker", "singularity", "mkdir"):
        stub = bin_dir / name
        stub.write_text(STUB)
        stub.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    result = subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=10,
                            env={"PATH": f"{bin_dir}:/usr/bin:/bin", "STUB_LOG": str(log)})
    assert result.returncode == 0, result.stderr
    assert not os.path.exists(MARKER), "a task value was executed by the host shell"
    return [json.loads(line) for line in log.read_text().splitlines()]


def _runtime_call(calls):
    """The single docker/singularity invocation among the recorded calls."""
    runtime_calls = [call for call in calls if call["argv"][0] in ("docker", "singularity")]
    assert len(runtime_calls) == 1, calls
    return runtime_calls[0]


def _builder(container_type, image="ubuntu"):
    return ContainerCommandBuilder(container_type).with_image(image).with_command(["true"])


def _env_args(container_type, call):
    """Environment passed to the container, as {name: value}."""
    if container_type == "docker":
        argv = call["argv"]
        return dict(arg[len("--env="):].split("=", 1) for arg in argv if arg.startswith("--env="))
    envs = {prefix: {k[len(prefix):]: v for k, v in call["env"].items() if k.startswith(prefix)}
            for prefix in ("APPTAINERENV_", "SINGULARITYENV_")}
    # Apptainer reads both prefixes and only accepts them silently when the values are equal
    assert envs["APPTAINERENV_"] == envs["SINGULARITYENV_"]
    return envs["SINGULARITYENV_"]


# --- env ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("container_type", CONTAINER_TYPES)
@pytest.mark.parametrize("value", HOSTILE, ids=HOSTILE_IDS)
def test_env_value_is_passed_intact(container_type, value, tmp_path):
    command = _builder(container_type).with_env("VAR", value).get_run_command()

    tokens = shlex.split(command)
    expected = f"--env=VAR={value}" if container_type == "docker" else f"SINGULARITYENV_VAR={value}"
    assert expected in tokens

    call = _runtime_call(_run_with_stubs(command, tmp_path))
    assert _env_args(container_type, call) == {"VAR": value}


@pytest.mark.parametrize("container_type", CONTAINER_TYPES)
def test_several_env_vars_are_passed_intact(container_type, tmp_path):
    envs = {f"VAR_{i}": value for i, value in enumerate(HOSTILE)}
    builder = _builder(container_type)
    for name, value in envs.items():
        builder.with_env(name, value)

    call = _runtime_call(_run_with_stubs(builder.get_run_command(), tmp_path))
    assert _env_args(container_type, call) == envs


@pytest.mark.parametrize("container_type", CONTAINER_TYPES)
@pytest.mark.parametrize("name", [
    "", "1VAR", "VAR-NAME", "VAR NAME", "VAR=X", "$(id)", "`id`", "A;B", "A\nB", "VÄR", "VAR\n", "--privileged",
])
def test_invalid_env_name_is_rejected(container_type, name):
    with pytest.raises(ValueError):
        _builder(container_type).with_env(name, "value").get_run_command()


def test_singularity_does_not_evaluate_env_values():
    # Apptainer shell-evaluates injected env values inside the container unless --no-eval is given
    tokens = shlex.split(_builder("singularity").with_env("VAR", "$(id)").get_run_command())
    assert tokens[tokens.index("exec") + 1] == "--no-eval"


# --- workdir -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("container_type", CONTAINER_TYPES)
@pytest.mark.parametrize("workdir", HOSTILE_PATHS, ids=HOSTILE_IDS)
def test_workdir_is_passed_intact(container_type, workdir, tmp_path):
    command = _builder(container_type).with_workdir(workdir).get_run_command()

    flag = "--workdir" if container_type == "docker" else "--pwd"
    assert f"{flag}={workdir}" in shlex.split(command)
    assert f"{flag}={workdir}" in _runtime_call(_run_with_stubs(command, tmp_path))["argv"]


# --- image -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("container_type", CONTAINER_TYPES)
@pytest.mark.parametrize("image", [value for value in HOSTILE if value and not value.startswith("-")],
                         ids=[i for v, i in zip(HOSTILE, HOSTILE_IDS) if v and not v.startswith("-")])
def test_image_is_passed_intact(container_type, image, tmp_path):
    command = _builder(container_type, image).get_run_command()

    # singularity runs every command through `sh -c`
    expected = [image, "true"] if container_type == "docker" else [f"docker://{image}", "sh", "-c", "true"]
    assert shlex.split(command)[-len(expected):] == expected
    assert _runtime_call(_run_with_stubs(command, tmp_path))["argv"][-len(expected):] == expected


@pytest.mark.parametrize("container_type", CONTAINER_TYPES)
@pytest.mark.parametrize("image", ["--privileged", "-v", "-"])
def test_image_starting_with_dash_is_rejected(container_type, image):
    # An image like "--privileged" would be parsed as an option of `docker run`.
    with pytest.raises(ValueError):
        _builder(container_type, image).get_run_command()


# --- mounts ------------------------------------------------------------------------------------------------

def _mount_args(container_type, argv):
    prefix = "--volume=" if container_type == "docker" else "--bind="
    return [arg[len(prefix):] for arg in argv if arg.startswith(prefix)]


@pytest.mark.parametrize("container_type", CONTAINER_TYPES)
@pytest.mark.parametrize("path", HOSTILE_PATHS, ids=HOSTILE_IDS)
def test_bind_mount_paths_are_passed_intact(container_type, path, tmp_path):
    host_path = f"/staging/{path}"
    command = _builder(container_type).with_bind_mount(path, host_path).get_run_command()

    assert _mount_args(container_type, shlex.split(command)) == [f"{host_path}:{path}"]
    argv = _runtime_call(_run_with_stubs(command, tmp_path))["argv"]
    assert _mount_args(container_type, argv) == [f"{host_path}:{path}"]


@pytest.mark.parametrize("container_type", CONTAINER_TYPES)
@pytest.mark.parametrize("path", HOSTILE_PATHS, ids=HOSTILE_IDS)
def test_volume_and_directory_paths_are_passed_intact(container_type, path, tmp_path):
    volume_name = f"vol-{path}"
    command = _builder(container_type) \
        .with_volume(path, volume_name) \
        .with_directory(f"/staging/{path}") \
        .get_run_command()

    calls = _run_with_stubs(command, tmp_path)
    assert _mount_args(container_type, _runtime_call(calls)["argv"]) == [f"{volume_name}:{path}"]
    if container_type == "singularity":
        assert [call["argv"] for call in calls if call["argv"][0] == "mkdir"] == [
            ["mkdir", "-p", "--", f"/staging/{path}"],
            ["mkdir", "-p", "--", volume_name],
        ]


@pytest.mark.parametrize("container_type", CONTAINER_TYPES)
@pytest.mark.parametrize("path", ["/data/a:b", "/data/a,/etc:/host-etc"])
def test_mount_path_with_separator_is_rejected(container_type, path):
    # ':' and ',' separate mount specs and options for `docker -v` / `singularity -B`.
    with pytest.raises(ValueError):
        _builder(container_type).with_bind_mount(path, "/staging/input_file_0").get_run_command()
    with pytest.raises(ValueError):
        _builder(container_type).with_volume(path, "vol-x").get_run_command()


# --- unified command functions -----------------------------------------------------------------------------

def _executor(value):
    return TesTaskExecutor(image="ubuntu", command=["sh", "-c", 'printf "%s" "$VAR"'],
                           workdir=f"/work/{value}", env={"VAR": value})


@pytest.mark.parametrize("container_type", CONTAINER_TYPES)
@pytest.mark.parametrize("value", HOSTILE, ids=HOSTILE_IDS)
def test_run_command_passes_task_values_intact(container_type, value, tmp_path):
    executor = _executor(value)
    volume_confs = [{'volume_name': f"vol-job-out{value}", 'container_path': f"/out{value}"}]
    input_confs = [{'container_path': f"/in/{value}", 'pulsar_path': "/staging/inputs/input_file_0",
                    'url': "http://example.org/in", 'type': TesTaskIOType.FILE}]

    command = run_command(executor, "job", {}, volume_confs, input_confs, [], "/staging/inputs", container_type)

    call = _runtime_call(_run_with_stubs(command, tmp_path))
    argv = call["argv"]
    assert _env_args(container_type, call) == {"VAR": value}
    assert ("--workdir=" if container_type == "docker" else "--pwd=") + str(executor.workdir) in argv
    assert sorted(_mount_args(container_type, argv)) == sorted([
        f"vol-job-out{value}:/out{value}", f"/staging/inputs/input_file_0:/in/{value}"])


@pytest.mark.parametrize("container_type", CONTAINER_TYPES)
@pytest.mark.parametrize("value", HOSTILE, ids=HOSTILE_IDS)
def test_stage_in_passes_url_intact(container_type, value, tmp_path):
    url = f"http://example.org/{value}"
    stage_exec = TesTaskExecutor(image="curl-image", command=[], workdir=Path("/downloads"))
    input_confs = [{'container_path': '/data/in', 'pulsar_path': '/staging/inputs/input_file_0',
                    'url': url, 'type': TesTaskIOType.FILE}]

    command = stage_in_command(stage_exec, {}, "/staging/inputs", input_confs, container_type)

    argv = _runtime_call(_run_with_stubs(command, tmp_path))["argv"]
    assert argv[-3:-1] == ["sh", "-c"]
    assert shlex.split(argv[-1]) == ["curl", "-fgL", "--max-redirs", str(MAX_REDIRECTS), "--proto-redir",
                                     "=http,https", "-o", "input_file_0", url]


@pytest.mark.parametrize("container_type", CONTAINER_TYPES)
@pytest.mark.parametrize("value", HOSTILE, ids=HOSTILE_IDS)
def test_stage_out_passes_values_intact(container_type, value, tmp_path):
    stage_exec = TesTaskExecutor(image="curl-image", command=[], workdir=Path("/downloads"),
                                 env={"VAR": value})
    output_confs = [{'container_path': f"/out/{value}", 'url': "http://example.org/upload",
                     'volume_name': "vol-job-out", 'type': TesTaskIOType.FILE}]
    volume_confs = [{'volume_name': f"vol-job-out{value}", 'container_path': f"/out{value}"}]

    command = stage_out_command(stage_exec, {}, output_confs, volume_confs, container_type,
                                bind_mount="/staging/inputs")

    call = _runtime_call(_run_with_stubs(command, tmp_path))
    assert _env_args(container_type, call) == {"VAR": value}
    assert f"vol-job-out{value}:/out{value}" in _mount_args(container_type, call["argv"])


# --- stage-out upload script (issue #4) -----------------------------------------------------------------------

CURL_STUB = f"""#!{sys.executable}
import json, os, sys
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps({{"argv": ["curl", *sys.argv[1:]], "cwd": os.getcwd()}}) + "\\n")
"""


def _hostile_output_confs(value, root):
    """HTTP, presigned S3 PUT and presigned S3 POST outputs whose paths, URLs, keys and form fields hold `value`."""
    return [
        {'container_path': f"{root}/file{value}", 'url': f"http://example.org/upload/{value}",
         'volume_name': "vol-job-out", 'type': TesTaskIOType.FILE},
        {'container_path': f"{root}/put{value}", 'url': f"https://s3.example.org/b/{value}?X-Amz-Signature={value}",
         'volume_name': "vol-job-out", 'type': TesTaskIOType.FILE, 'upload': 's3_put', 'presigned': True},
        {'container_path': f"{root}/dir{value}", 'url': f"https://s3.example.org/{value}",
         'volume_name': "vol-job-out", 'type': TesTaskIOType.DIRECTORY, 'upload': 's3_post', 'presigned': True,
         'key_prefix': f"work/{value}/", 'form_fields': {'policy': value, 'x-amz-signature': value}},
        {'container_path': f"{root}/httpdir{value}", 'url': f"http://example.org/{value}",
         'volume_name': "vol-job-out", 'type': TesTaskIOType.DIRECTORY},
    ]


@pytest.mark.parametrize("container_type", CONTAINER_TYPES)
@pytest.mark.parametrize("value", HOSTILE, ids=HOSTILE_IDS)
def test_stage_out_script_reaches_the_container_intact(container_type, value, tmp_path):
    stage_exec = TesTaskExecutor(image="curl-image", command=[], workdir=Path("/downloads"))
    output_confs = _hostile_output_confs(value, "/out")
    volume_confs = [{'volume_name': "vol-job-out", 'container_path': "/out"}]

    command = stage_out_command(stage_exec, {}, output_confs, volume_confs, container_type,
                                bind_mount="/staging/inputs")

    argv = _runtime_call(_run_with_stubs(command, tmp_path))["argv"]
    assert argv[-3:-1] == ["sh", "-c"]
    assert argv[-1] == stage_out_script(output_confs)


@pytest.mark.parametrize("value", HOSTILE, ids=HOSTILE_IDS)
def test_stage_out_script_passes_paths_urls_and_keys_intact(value, tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    name = value.replace("/", "%")  # a file name cannot contain "/"
    output_confs = _hostile_output_confs(name, str(root))
    for conf in output_confs:
        path = Path(conf['container_path'])
        if conf['type'] == TesTaskIOType.DIRECTORY:
            (path / "sub").mkdir(parents=True)
            (path / "sub" / f"f{name}").write_text("x")
        else:
            path.write_text("x")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(CURL_STUB)
    (bin_dir / "curl").chmod(0o755)
    log = tmp_path / "curl.jsonl"

    result = subprocess.run(["sh", "-c", stage_out_script(output_confs)], capture_output=True, text=True,
                            timeout=30, env={"PATH": f"{bin_dir}:/usr/bin:/bin", "STUB_LOG": str(log)})

    assert result.returncode == 0, result.stderr
    assert not os.path.exists(MARKER), "a task value was executed by the shell"
    file_call, put_call, post_call, http_dir_call = [json.loads(line)["argv"] for line in log.read_text().splitlines()]
    assert file_call[-1] == output_confs[0]['url']
    assert put_call[put_call.index("-T") + 1] == output_confs[1]['container_path']
    assert put_call[-1] == output_confs[1]['url']
    assert post_call[-1] == output_confs[2]['url']
    form_strings = [post_call[i + 1] for i, arg in enumerate(post_call) if arg == "--form-string"]
    assert form_strings == [f"key=work/{name}/sub/f{name}", f"policy={name}", f"x-amz-signature={name}"]
    assert http_dir_call[-1] == output_confs[3]['url']
