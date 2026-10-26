"""TES `resources` (cpu_cores, ram_gb): container limit flags per runtime and setting, and the per-job DRMAA native
specification sent to Pulsar.

Apptainer only accepts `--cpus`/`--memory` with a usable cgroups manager, which hosts without systemd or cgroups
delegation lack (issue #38), so singularity gets no limit flags unless `container.resource_limits = "always"`.
"""
import asyncio
import json
import shlex
from pathlib import Path

import pytest

from tesp_api.repository.model.task import TesTaskExecutor
from tesp_api.service.pulsar_operations import PulsarAmqpOperations, PulsarRestOperations
from tesp_api.service.resources import ResourceConfig, load_resource_config
from tesp_api.utils.container import ContainerCommandBuilder, run_command

RESOURCES = {'cpu_cores': 2, 'ram_gb': 1.5}


def _run_command(container_type: str, resource_conf: dict) -> list:
    executor = TesTaskExecutor(image="alpine", command=["true"], workdir=Path("/work"))
    cmd = run_command(executor=executor, job_id="job", resource_conf=resource_conf, volume_confs=[], input_confs=[],
                      output_confs=[], inputs_directory="", container_type=container_type)
    return shlex.split(cmd)


# --- Flags on the container command line --------------------------------------------------------------------------

@pytest.mark.parametrize("container_type", ["docker", "singularity"])
def test_limit_flags(container_type):
    argv = _run_command(container_type, RESOURCES)
    assert "--cpus=2" in argv
    assert "--memory=1536m" in argv  # whole MiB, accepted by docker and apptainer
    assert not any(arg.startswith("--cpu=") for arg in argv)  # apptainer has no --cpu (issue #38)


@pytest.mark.parametrize("container_type", ["docker", "singularity"])
def test_no_flags_without_resources(container_type):
    argv = _run_command(container_type, {'cpu_cores': None, 'ram_gb': None})
    assert not any(arg.startswith(("--cpus", "--memory")) for arg in argv)


@pytest.mark.parametrize("ram_gb, flag", [(1, "--memory=1024m"), (0.5, "--memory=512m"),
                                          (1e-6, "--memory=1m"), (2.0001, "--memory=2049m")])
def test_memory_rounds_up_to_whole_mib(ram_gb, flag):
    argv = shlex.split(ContainerCommandBuilder("docker").with_image("alpine")
                       .with_resource({'ram_gb': ram_gb}).get_run_command())
    assert flag in argv


@pytest.mark.parametrize("resources", [{'cpu_cores': 0, 'ram_gb': 0}, {'cpu_cores': -1, 'ram_gb': -2}])
def test_non_positive_resources_are_ignored(resources):
    argv = shlex.split(ContainerCommandBuilder("docker").with_image("alpine")
                       .with_resource(resources).get_run_command())
    assert not any(arg.startswith(("--cpus", "--memory")) for arg in argv)


# --- container.resource_limits ------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode, container_type, applied", [
    ("auto", "docker", True),
    ("auto", "singularity", False),
    ("always", "docker", True),
    ("always", "singularity", True),
    ("never", "docker", False),
    ("never", "singularity", False),
])
def test_container_resource_conf(mode, container_type, applied):
    conf = ResourceConfig(container_limits=mode).container_resource_conf(RESOURCES, container_type)
    assert conf == (RESOURCES if applied else {})
    argv = _run_command(container_type, conf)
    assert ("--cpus=2" in argv) == applied
    assert ("--memory=1536m" in argv) == applied


def test_default_mode_is_auto():
    assert ResourceConfig().container_limits == "auto"
    assert load_resource_config({}).container_limits == "auto"


def test_invalid_mode_is_rejected():
    with pytest.raises(ValueError, match="container.resource_limits"):
        ResourceConfig(container_limits="sometimes")


def test_load_from_settings():
    config = load_resource_config({
        'container': {'resource_limits': 'Never'},
        'pulsar': {'native_specification': {'cpu_cores': '--cpus-per-task={cpu_cores}', 'ram_gb': '--mem={ram_mb}'}},
    })
    assert config == ResourceConfig(container_limits='never', native_spec_cpu_cores='--cpus-per-task={cpu_cores}',
                                    native_spec_ram_gb='--mem={ram_mb}')


# --- Native specification (e.g. SLURM through Pulsar's DRMAA manager) ----------------------------------------------

SLURM = ResourceConfig(native_spec_cpu_cores="--cpus-per-task={cpu_cores}", native_spec_ram_gb="--mem={ram_mb}")


def test_no_native_specification_by_default():
    assert ResourceConfig().native_specification(RESOURCES) == ""
    assert ResourceConfig().submit_params(RESOURCES) == {}


def test_native_specification():
    assert SLURM.native_specification(RESOURCES) == "--cpus-per-task=2 --mem=1536"
    assert SLURM.submit_params(RESOURCES) == {'native_specification': "--cpus-per-task=2 --mem=1536"}


@pytest.mark.parametrize("resources, expected", [
    ({'cpu_cores': 4, 'ram_gb': None}, "--cpus-per-task=4"),
    ({'cpu_cores': None, 'ram_gb': 2}, "--mem=2048"),
    ({'cpu_cores': 0, 'ram_gb': 0}, ""),
    ({}, ""),
    (None, ""),
])
def test_native_specification_omits_missing_resources(resources, expected):
    assert SLURM.native_specification(resources) == expected
    assert SLURM.submit_params(resources) == ({'native_specification': expected} if expected else {})


def test_native_specification_placeholders():
    config = ResourceConfig(native_spec_cpu_cores="-pe smp {cpu_cores}", native_spec_ram_gb="-l h_vmem={ram_gb}G")
    assert config.native_specification({'cpu_cores': 3, 'ram_gb': 0.5}) == "-pe smp 3 -l h_vmem=0.5G"


def test_native_specification_is_independent_of_container_limits():
    config = ResourceConfig(container_limits="never", native_spec_cpu_cores="--cpus-per-task={cpu_cores}")
    assert config.native_specification(RESOURCES) == "--cpus-per-task=2"


@pytest.mark.parametrize("template", ["--mem={memory}", "--mem={0}", "--mem={ram_mb", "--mem={ram_mb.real.x}"])
def test_invalid_template_is_rejected(template):
    with pytest.raises(ValueError, match="pulsar.native_specification.ram_gb"):
        ResourceConfig(native_spec_ram_gb=template)


# --- Submit parameters reach Pulsar ---------------------------------------------------------------------------------

def _run(promise):
    async def wait():
        return await promise
    return asyncio.run(wait())


def _record_rest_requests(ops: PulsarRestOperations) -> list:
    calls = []

    async def fake_request(**kwargs):
        calls.append(kwargs)
        return b""
    ops._pulsar_request = fake_request
    return calls


def test_rest_submit_sends_params():
    ops = PulsarRestOperations(None, "http://pulsar", 1, 1)
    calls = _record_rest_requests(ops)
    _run(ops.run_job("job", "true", {'native_specification': "--cpus-per-task=2"}))
    assert calls[0]['path'] == "/jobs/job/submit"
    assert calls[0]['params']['command_line'] == "true"
    assert json.loads(calls[0]['params']['params']) == {'native_specification': "--cpus-per-task=2"}


def test_rest_submit_without_params():
    ops = PulsarRestOperations(None, "http://pulsar", 1, 1)
    calls = _record_rest_requests(ops)
    _run(ops.run_job("job", "true"))
    assert calls[0]['params'] == {'command_line': "true"}


@pytest.mark.parametrize("submit_params", [{}, {'native_specification': "--mem=1024"}])
def test_amqp_submit_sends_params(submit_params):
    ops = PulsarAmqpOperations("amqp://x", None, "http://pulsar", 1, 1)
    messages = []

    async def fake_send(message, routing_key="pulsar.queue"):
        messages.append(message)
    ops._send_message = fake_send
    _run(ops.run_job("job", "true", submit_params))
    assert messages[0]['command_line'] == "true"
    assert messages[0].get('submit_params', {}) == submit_params
