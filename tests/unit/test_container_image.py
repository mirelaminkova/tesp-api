"""Executor images that would make the container runtime use files on the Pulsar host are refused.

`singularity exec` runs an absolute path (a directory tree or a `.sif` file) as the container image, as the Pulsar
user, and `instance://` attaches to a running instance. Images must name a registry image instead: a Docker
reference (prefixed with `docker://` for Singularity) or a `docker://`, `library://`, `shub://` or `oras://` URI.
"""
import pytest

from tesp_api.repository.model.task import TesTask, find_invalid_container_fields, is_valid_image, \
    REGISTRY_IMAGE_SCHEMES
from tesp_api.utils.container import ContainerCommandBuilder

CONTAINER_TYPES = ["docker", "singularity"]

HOST_IMAGES = ["/", "/home/x", "/images/../etc", "/images/x.sif", "./rel", "../rel", ".", "..", ".hidden",
               "instance://x", "INSTANCE://x", "DOCKER://alpine", "file:///etc", "docker-daemon://x",
               "oci-archive://x.tar", "https://depot.galaxyproject.org/singularity/x.sif", "--privileged", ""]
REGISTRY_IMAGES = ["docker://alpine", "library://alpine", "shub://org/img", "oras://ghcr.io/org/img:1",
                   "alpine:3.20", "ubuntu", "quay.io/org/img:1.0", "myregistryhost:5000/fedora/httpd:1.0",
                   "alpine@sha256:" + "0" * 64, "rel/path"]
URI_SCHEMES = tuple(f"{scheme}://" for scheme in REGISTRY_IMAGE_SCHEMES)


@pytest.mark.parametrize("image", HOST_IMAGES)
def test_host_image_is_invalid(image):
    assert not is_valid_image(image)


@pytest.mark.parametrize("image", REGISTRY_IMAGES)
def test_registry_image_is_valid(image):
    assert is_valid_image(image)


@pytest.mark.parametrize("image", HOST_IMAGES)
def test_host_image_names_the_field(image):
    task = TesTask(executors=[{"image": "ubuntu", "command": ["true"]}, {"image": image, "command": ["true"]}])
    assert find_invalid_container_fields(task) == ["executors[1].image"]


@pytest.mark.parametrize("image", REGISTRY_IMAGES)
def test_registry_image_has_no_invalid_fields(image):
    task = TesTask(executors=[{"image": image, "command": ["true"]}])
    assert find_invalid_container_fields(task) == []


@pytest.mark.parametrize("container_type", CONTAINER_TYPES)
@pytest.mark.parametrize("image", HOST_IMAGES)
def test_builder_rejects_host_image(container_type, image):
    # Tasks stored before the creation-time check reach the builder unchecked.
    with pytest.raises(ValueError) as error:
        ContainerCommandBuilder(container_type).with_image(image)
    if len(image) > 1:  # "/" and "." are in the message itself
        assert image not in str(error.value)


@pytest.mark.parametrize("container_type", CONTAINER_TYPES)
@pytest.mark.parametrize("image", REGISTRY_IMAGES)
def test_builder_accepts_registry_image(container_type, image):
    # Docker gets URIs as-is too and fails to pull them, as before
    built = ContainerCommandBuilder(container_type).with_image(image)._image.value
    if container_type == "docker" or image.startswith(URI_SCHEMES):
        assert built == image
    else:
        assert built == f"docker://{image}"
