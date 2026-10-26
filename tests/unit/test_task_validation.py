import pytest

from tesp_api.repository.model.task import TesTask, find_invalid_container_fields


def _task(**overrides):
    executor = {"image": "ubuntu", "command": ["true"], **overrides.pop("executor", {})}
    return TesTask(executors=[{"image": "ubuntu", "command": ["true"]}, executor], **overrides)


def test_valid_task_has_no_invalid_fields():
    task = _task(executor={"env": {"_A": "$(x)", "b2": "`y`"}, "image": "quay.io/org/img:1.0"},
                 inputs=[{"path": "/data/in file", "url": "http://example.org/in"}],
                 outputs=[{"path": "/data/out", "url": "http://example.org/upload", "type": "FILE"}],
                 volumes=["/vol/A"])
    assert find_invalid_container_fields(task) == []


@pytest.mark.parametrize("name", ["1A", "A-B", "A B", "A;B", "$(id)", "A\n", "", "VÄR"])
def test_invalid_env_name_names_the_field(name):
    assert find_invalid_container_fields(_task(executor={"env": {"OK": "1", name: "x"}})) == ["executors[1].env"]


def test_image_starting_with_dash_names_the_field():
    assert find_invalid_container_fields(_task(executor={"image": "--privileged"})) == ["executors[1].image"]


@pytest.mark.parametrize("path", ["/data/a:b", "/data/a,/etc:/x"])
def test_mount_separator_in_paths_names_the_field(path):
    task = _task(inputs=[{"path": "/ok", "url": "http://example.org/in"}, {"path": path, "content": "x"}],
                 volumes=["/ok", path])
    assert find_invalid_container_fields(task) == ["inputs[1].path", "volumes[1]"]
    task = _task(outputs=[{"path": f"{path}/out", "url": "http://example.org/upload", "type": "FILE"}])
    assert find_invalid_container_fields(task) == ["outputs[0].path"]


def test_mount_separator_in_output_file_name_is_allowed():
    # Outputs are mounted by their directory only
    task = _task(outputs=[{"path": "/data/sample:1,2.txt", "url": "http://example.org/upload", "type": "FILE"}])
    assert find_invalid_container_fields(task) == []
