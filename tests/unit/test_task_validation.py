import pytest

from tesp_api.repository.model.task import TesTask, find_invalid_container_fields, find_relative_paths, \
    find_nul_fields


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


def test_absolute_paths_are_accepted():
    task = _task(executor={"workdir": "/data"}, inputs=[{"path": "/data/in", "content": "x"}], volumes=["/vol/A"])
    assert find_relative_paths(task) == []


@pytest.mark.parametrize("path", ["in.txt", "./in", "data/in", ""])
def test_relative_input_path_names_the_field(path):
    task = _task(inputs=[{"path": "/ok", "content": "x"}, {"path": path, "content": "x"}])
    assert find_relative_paths(task) == ["inputs[1].path"]


@pytest.mark.parametrize("path", ["vol", "./vol", "a/b"])
def test_relative_volume_names_the_field(path):
    assert find_relative_paths(_task(volumes=["/ok", path])) == ["volumes[1]"]


@pytest.mark.parametrize("path", ["data", "./data", ""])
def test_relative_workdir_names_the_field(path):
    assert find_relative_paths(_task(executor={"workdir": path})) == ["executors[1].workdir"]


def test_task_without_nul_has_no_nul_fields():
    task = _task(executor={"env": {"A": "a b"}, "stdout": "/out"}, name="n", tags={"k": "v"},
                 inputs=[{"path": "/in", "url": "http://example.org/in"}], volumes=["/vol"])
    assert find_nul_fields(task) == []


@pytest.mark.parametrize("overrides, field", [
    ({"name": "a\x00b"}, "name"),
    ({"description": "a\x00b"}, "description"),
    ({"executor": {"image": "ubuntu\x00"}}, "executors[1].image"),
    ({"executor": {"command": ["echo", "a\x00b"]}}, "executors[1].command[1]"),
    ({"executor": {"env": {"A": "a\x00b"}}}, "executors[1].env"),
    ({"executor": {"workdir": "/a\x00b"}}, "executors[1].workdir"),
    ({"executor": {"stdout": "/a\x00b"}}, "executors[1].stdout"),
    ({"inputs": [{"path": "/in\x00", "content": "x"}]}, "inputs[0].path"),
    ({"outputs": [{"path": "/out\x00", "url": "http://example.org/upload", "type": "FILE"}]}, "outputs[0].path"),
    ({"volumes": ["/vol\x00"]}, "volumes[0]"),
    ({"tags": {"k": "a\x00b"}}, "tags"),
    ({"tags": {"k\x00": "v"}}, "tags"),
    ({"resources": {"zones": ["z\x00"]}}, "resources.zones[0]"),
])
def test_nul_names_the_field(overrides, field):
    assert find_nul_fields(_task(**overrides)) == [field]


def test_nul_in_input_content_is_allowed():
    # Content is uploaded to Pulsar as a file, not passed through the job script
    assert find_nul_fields(_task(inputs=[{"path": "/in", "content": "a\x00b"}])) == []
