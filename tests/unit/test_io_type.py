import pytest

from tesp_api.repository.model.task import TesTask, TesTaskInput, TesTaskIOType, TesTaskOutput


def test_output_type_defaults_to_file():
    output = TesTaskOutput(path="/data/out", url="http://example.org/upload")
    assert output.type == TesTaskIOType.FILE


def test_input_type_defaults_to_file():
    assert TesTaskInput(path="/data/in", url="http://example.org/in").type == TesTaskIOType.FILE


def test_task_without_io_types_is_valid():
    task = TesTask.parse_obj({
        "inputs": [{"path": "/data/in", "url": "http://example.org/in"}],
        "outputs": [{"path": "/data/out", "url": "http://example.org/upload"}],
        "executors": [{"image": "ubuntu", "command": ["true"]}],
    })
    assert task.inputs[0].type == TesTaskIOType.FILE
    assert task.outputs[0].type == TesTaskIOType.FILE
    assert task.dict()["outputs"][0]["type"] == TesTaskIOType.FILE


@pytest.mark.parametrize("io_type", ["FILE", "DIRECTORY"])
def test_explicit_output_type_is_kept(io_type):
    assert TesTaskOutput(path="/data/out", url="http://example.org/upload", type=io_type).type == io_type
