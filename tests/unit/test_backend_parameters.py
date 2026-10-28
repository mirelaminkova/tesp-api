import json

import pytest

from tesp_api.repository.model.task import TesTask, RegisteredTesTask, \
    find_unsupported_backend_parameters

PARAMETERS = {"INVALID": "PARAMETER", "VmSize": "Standard_D64_v3"}


def _task(resources):
    return TesTask(executors=[{"image": "alpine", "command": ["true"]}], resources=resources)


def test_strict_task_with_unsupported_parameters_is_refused():
    task = _task({"backend_parameters": PARAMETERS, "backend_parameters_strict": True})
    assert find_unsupported_backend_parameters(task) == ["INVALID", "VmSize"]


@pytest.mark.parametrize("resources", [
    None,
    {},
    {"backend_parameters": PARAMETERS},
    {"backend_parameters": PARAMETERS, "backend_parameters_strict": False},
    {"backend_parameters": {}, "backend_parameters_strict": True},
    {"backend_parameters_strict": True},
])
def test_non_strict_or_empty_parameters_are_accepted(resources):
    assert find_unsupported_backend_parameters(_task(resources)) == []


def test_strict_defaults_to_false():
    assert _task({"backend_parameters": PARAMETERS}).resources.backend_parameters_strict is False


@pytest.mark.parametrize("strict", [False, True])
def test_parameters_are_stored_and_returned_in_full_view(strict):
    resources = {"backend_parameters": {"VmSize": "Standard_D64_v3"}, "backend_parameters_strict": strict}
    stored = RegisteredTesTask(**_task(resources).dict())
    # The FULL view excludes nothing (endpoint_utils.get_view, not importable here: motor needs asyncio.coroutine)
    full = json.loads(stored.json())
    assert full["resources"]["backend_parameters"] == {"VmSize": "Standard_D64_v3"}
    assert full["resources"]["backend_parameters_strict"] is strict
