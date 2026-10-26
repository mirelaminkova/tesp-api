from pathlib import Path

import pytest

from tesp_api.repository.model.task import TesTaskExecutor, TesTaskIOType
from tesp_api.utils.container import stage_in_command

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
