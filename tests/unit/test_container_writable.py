"""Unit tests for writable Apptainer executor containers (issue #68).

Docker gives each executor a writable container layer. Apptainer's container filesystem, and the directories it creates
for input mounts, are read-only unless the container gets a writable tmpfs overlay (``--writable-tmpfs``).
"""
import shlex
import shutil
import subprocess

import pytest

from tesp_api.repository.model.task import TesTaskExecutor, TesTaskIOType
from tesp_api.utils.container import ContainerCommandBuilder, run_command, stage_in_command, stage_out_command

EXECUTOR = TesTaskExecutor(image="alpine:3.20", command=["sh", "-c", "mkdir -p /scratch && echo x > /data/new"])
STAGE = TesTaskExecutor(image="curl-image", command=[], workdir="/downloads")


def _input_confs(path="/staging/inputs/input_file_0"):
    return [{'container_path': '/data/in.txt', 'pulsar_path': path, 'url': "http://example.org/in",
             'type': TesTaskIOType.FILE}]


def _run_command(container_type, input_confs=None, volume_confs=()):
    return run_command(EXECUTOR, "job", {}, list(volume_confs), input_confs or _input_confs(), [], "/staging/inputs",
                       container_type)


def test_apptainer_executor_gets_a_writable_overlay():
    argv = shlex.split(_run_command("singularity"))
    assert argv[:4] == ["singularity", "exec", "--no-eval", "--writable-tmpfs"]
    assert argv.count("--writable-tmpfs") == 1


def test_docker_executor_is_unchanged():
    assert "--writable-tmpfs" not in _run_command("docker")


def test_stage_containers_get_no_overlay():
    # They write only into bound host directories.
    stage_in = stage_in_command(STAGE, {}, "/staging/inputs", _input_confs(), "singularity")
    stage_out = stage_out_command(STAGE, {}, [{'container_path': '/data/out.txt', 'url': "http://example.org/up",
                                               'type': TesTaskIOType.FILE}], [], "singularity", "/staging/inputs")
    assert "--writable-tmpfs" not in stage_in
    assert "--writable-tmpfs" not in stage_out


def test_reset_clears_the_overlay():
    builder = ContainerCommandBuilder("singularity").with_image("ubuntu").with_writable_tmpfs()
    builder.reset()
    assert "--writable-tmpfs" not in builder.with_image("ubuntu").get_run_command()


@pytest.mark.skipif(shutil.which("singularity") is None, reason="needs Apptainer/Singularity and a docker:// pull")
def test_apptainer_executor_can_write_next_to_its_inputs(tmp_path):
    staged = tmp_path / "input_file_0"
    staged.write_text("input\n")
    volume = tmp_path / "vol"
    command = _run_command("singularity", _input_confs(str(staged)),
                           [{'volume_name': str(volume), 'container_path': '/vol'}])
    result = subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr
    assert staged.read_text() == "input\n"  # the input itself is left alone
