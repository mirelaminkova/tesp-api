import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tesp_api.repository.model.task import TesTaskExecutor, TesTaskIOType
from tesp_api.utils.container import ContainerCommandBuilder, run_command, stage_in_command, stage_out_command, stage_out_script
from tesp_api.utils.job_script import job_script

STAGE_EXEC = TesTaskExecutor(image="curl-image", command=[], workdir=Path("/downloads"))


@pytest.mark.parametrize("container_type", ["docker", "singularity"])
def test_no_stage_in_when_all_inputs_are_inline(container_type):
    # Inline `content` inputs are uploaded to Pulsar directly; there is nothing to download,
    # and a container started without a command fails (singularity exec) or runs the image default.
    input_confs = [{'container_path': '/data/in', 'pulsar_path': '/staging/inputs/input_file_0',
                    'url': None, 'type': TesTaskIOType.FILE}]
    assert stage_in_command(STAGE_EXEC, {}, "/staging/inputs", input_confs, container_type) == ""


@pytest.mark.parametrize("container_type", ["docker", "singularity"])
def test_stage_in_downloads_url_inputs(container_type):
    input_confs = [{'container_path': '/data/in', 'pulsar_path': '/staging/inputs/input_file_0',
                    'url': 'http://example.org/in.txt', 'type': TesTaskIOType.FILE}]
    cmd = stage_in_command(STAGE_EXEC, {}, "/staging/inputs", input_confs, container_type)
    assert "curl -f -o input_file_0 http://example.org/in.txt" in cmd


# --- signed URLs stay out of argv and leave no container behind (issue #32) ---------------------------------------

RUNTIME_STUB = f"""#!{sys.executable}
import json, os, sys
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps({{"argv": [os.path.basename(sys.argv[0]), *sys.argv[1:]]}}) + "\\n")
# Like the container: run the trailing `sh -s` with the script on stdin, in the shell under test.
os.execvp(os.environ["CONTAINER_SH"], [os.environ["CONTAINER_SH"], sys.argv[-1]])
"""
# Reads all of its stdin, as a command in the script might; it must not get the rest of the script.
CURL_STUB = f"""#!{sys.executable}
import json, os, sys
stdin = sys.stdin.read()
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps({{"argv": ["curl", *sys.argv[1:]], "stdin": stdin}}) + "\\n")
sys.exit(22 if "fail" in sys.argv[-1] else 0)
"""
SIGNED_URL = "https://s3.example.org/b/in.txt?X-Amz-Credential=AKIAEXAMPLE&X-Amz-Signature=0123456789abcdef"


def _confs(*urls):
    return [{'container_path': f'/data/in{i}', 'pulsar_path': f'/staging/inputs/input_file_{i}', 'url': url,
             'type': TesTaskIOType.FILE} for i, url in enumerate(urls)]


def _run(command, tmp_path, shell):
    """Runs ``command`` in a job script with ``shell``, which also stands in for the container's ``sh``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, stub in (("docker", RUNTIME_STUB), ("singularity", RUNTIME_STUB), ("curl", CURL_STUB)):
        (bin_dir / name).write_text(stub)
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "calls.jsonl"
    log.touch()
    # Pulsar runs the job script through a shell; the stage command is one of its `&&`-joined parts.
    result = subprocess.run([shell, "-c", job_script(command, "echo after")], capture_output=True, text=True,
                            timeout=30, cwd=tmp_path, stdin=subprocess.DEVNULL,
                            env={"PATH": f"{bin_dir}:/usr/bin:/bin", "STUB_LOG": str(log), "CONTAINER_SH": shell})
    return result, [json.loads(line) for line in log.read_text().splitlines()]


HOST_SHELLS = [shell for shell in ("sh", "bash") if shutil.which(shell)]


@pytest.mark.parametrize("shell", HOST_SHELLS)
@pytest.mark.parametrize("container_type", ["docker", "singularity"])
def test_stage_in_script_runs_from_stdin_and_never_reaches_argv(container_type, shell, tmp_path):
    command = stage_in_command(STAGE_EXEC, {}, str(tmp_path / "staging"), _confs(SIGNED_URL, "http://x/2"),
                               container_type)
    result, calls = _run(command, tmp_path, shell)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "after\n"
    runtime, *curls = calls
    assert runtime["argv"][0] == container_type
    assert runtime["argv"][-2:] == ["sh", "-s"]
    assert not any("X-Amz" in arg or "http" in arg for arg in runtime["argv"])
    assert ("--rm" in runtime["argv"]) == (container_type == "docker")
    # Both downloads ran; curl reads its stdin and gets nothing.
    assert [curl["argv"][-1] for curl in curls] == [SIGNED_URL, "http://x/2"]
    assert [curl["stdin"] for curl in curls] == ["", ""]


@pytest.mark.parametrize("shell", HOST_SHELLS)
@pytest.mark.parametrize("container_type", ["docker", "singularity"])
def test_multi_line_script_is_not_consumed_by_its_commands(container_type, shell, tmp_path):
    # bash reads a script from a pipe byte by byte; without the `{ ... }` group the first curl would read the
    # second line. (dash reads ahead, so it is not affected.)
    command = ContainerCommandBuilder(container_type).with_image("curl-image") \
        .with_stdin_script("curl http://x/1\ncurl http://x/2").with_auto_remove().get_run_command()
    result, calls = _run(command, tmp_path, shell)
    assert result.returncode == 0, result.stderr
    assert [(call["argv"][-1], call["stdin"]) for call in calls[1:]] == [("http://x/1", ""), ("http://x/2", "")]


@pytest.mark.parametrize("shell", HOST_SHELLS)
@pytest.mark.parametrize("container_type", ["docker", "singularity"])
def test_failed_stage_in_fails_the_job(container_type, shell, tmp_path):
    command = stage_in_command(STAGE_EXEC, {}, str(tmp_path / "staging"), _confs("http://x/fail", "http://x/2"),
                               container_type)
    result, calls = _run(command, tmp_path, shell)
    assert result.returncode == 22
    assert result.stdout == ""
    assert [call["argv"][-1] for call in calls[1:]] == ["http://x/fail"]


@pytest.mark.parametrize("container_type", ["docker", "singularity"])
def test_stage_out_script_is_fed_on_stdin(container_type):
    output_confs = [{'container_path': '/out/f', 'url': SIGNED_URL, 'volume_name': 'vol', 'type': TesTaskIOType.FILE,
                     'upload': 's3_put', 'presigned': True}]
    command = stage_out_command(STAGE_EXEC, {}, output_confs, [{'volume_name': 'vol', 'container_path': '/out'}],
                                container_type, bind_mount="/staging")
    tokens = shlex.split(command)
    start = tokens.index("printf")
    assert tokens[start:start + 5] == ["printf", "%s\\n", "{", stage_out_script(output_confs), "} </dev/null"]
    runtime = tokens[start + 6:]
    assert runtime[-2:] == ["sh", "-s"]
    assert not any("X-Amz" in token for token in runtime)
    if container_type == "docker":
        assert runtime[:4] == ["docker", "run", "--rm", "--interactive"]


def test_executor_containers_keep_their_command_and_stdin():
    executor = TesTaskExecutor(image="alpine", command=["cat"], workdir=Path("/work"))
    tokens = shlex.split(run_command(executor, "job", {}, [], [], [], "", "docker"))
    assert tokens == ["docker", "run", "--workdir=/work", "alpine", "cat"]
