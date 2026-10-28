"""Pre-pulled Singularity images from a configured host directory (issue #46, ``container.allowed_image_dir``).

Off by default, so host-path images stay refused (#27). When set, an absolute image path is accepted at task creation
only if it is lexically below the directory, and on the Pulsar host the job resolves it with ``realpath`` and runs it
only if it then is a regular file below the directory. The host-side tests run the built command line with ``sh``,
with a fake ``singularity`` on ``PATH`` that records the image it was given.
"""
import os
import shlex
import subprocess

import pytest

from tesp_api.repository.model.task import TesTask, find_invalid_container_fields, is_allowed_host_image, \
    is_valid_image
from tesp_api.service.image_dir import load_allowed_image_dir
from tesp_api.utils.container import ContainerCommandBuilder

IMAGE_DIR = "/images"
ACCEPTED = ["/images/x.sif", "/images/sub/x.sif", "/images/.x.sif"]
REFUSED = ["/images/../etc", "/images/sub/../../etc/x.sif", "/imagesX/x.sif", "/", "/images", "/images/",
           "/images/./x.sif", "/images//x.sif", "//images/x.sif", "/images/x.sif/", "./x.sif",
           "/etc/passwd"]


# --- Task creation (lexical) ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("image", ACCEPTED + REFUSED)
def test_host_images_are_refused_when_the_setting_is_off(image):
    assert not is_valid_image(image)
    assert not is_valid_image(image, None)
    assert not is_valid_image(image, "")


@pytest.mark.parametrize("image", ACCEPTED)
def test_images_below_the_directory_are_accepted(image):
    assert is_allowed_host_image(image, IMAGE_DIR)
    assert is_valid_image(image, IMAGE_DIR)
    assert is_valid_image(image, IMAGE_DIR + "/")


@pytest.mark.parametrize("image", REFUSED)
def test_other_host_paths_are_refused(image):
    assert not is_valid_image(image, IMAGE_DIR)


def test_registry_images_stay_valid_with_the_setting():
    assert is_valid_image("ubuntu", IMAGE_DIR)
    assert is_valid_image("docker://alpine", IMAGE_DIR)
    assert not is_valid_image("instance://x", IMAGE_DIR)


def test_task_fields():
    task = TesTask(executors=[{"image": "/images/x.sif", "command": ["true"]},
                              {"image": "/imagesX/x.sif", "command": ["true"]}])
    assert find_invalid_container_fields(task) == ["executors[0].image", "executors[1].image"]
    assert find_invalid_container_fields(task, IMAGE_DIR) == ["executors[1].image"]


# --- Setting ----------------------------------------------------------------------------------------------------------

def _props(value):
    return {"container": {"allowed_image_dir": value}}


def test_setting_is_off_by_default():
    assert load_allowed_image_dir({}, "singularity") is None
    assert load_allowed_image_dir(_props(""), "singularity") is None
    assert load_allowed_image_dir(_props(None), "singularity") is None


def test_setting_is_read():
    assert load_allowed_image_dir(_props("/srv/images"), "singularity") == "/srv/images"
    assert load_allowed_image_dir(_props(" /srv/images/ "), "singularity") == "/srv/images"


@pytest.mark.parametrize("value", ["/", "//", "srv/images", "./images", "/srv/../etc", "/srv/./images", "/srv//images"])
def test_invalid_setting_is_refused(value):
    with pytest.raises(ValueError, match="container.allowed_image_dir"):
        load_allowed_image_dir(_props(value), "singularity")


def test_setting_is_ignored_for_docker():
    assert load_allowed_image_dir(_props("/srv/images"), "docker") is None


def test_docker_builder_ignores_the_directory():
    with pytest.raises(ValueError):
        ContainerCommandBuilder("docker", IMAGE_DIR).with_image("/images/x.sif")


def test_repo_settings_toml_keeps_it_off():
    from tesp_api.config.properties import properties
    assert load_allowed_image_dir(properties, "singularity") is None


# --- Pulsar host (resolved) -------------------------------------------------------------------------------------------

@pytest.fixture
def host(tmp_path):
    """An image directory with a fake singularity on PATH that prints the image it is given."""
    images = tmp_path / "images"
    (images / "sub").mkdir(parents=True)
    (images / "x.sif").write_text("sif")
    (images / "sub" / "y.sif").write_text("sif")
    (images / "sandbox").mkdir()
    outside = tmp_path / "outside.sif"
    outside.write_text("sif")
    (images / "escape.sif").symlink_to(outside)
    (images / "inside.sif").symlink_to(images / "x.sif")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "singularity"
    # singularity exec --no-eval [options] IMAGE COMMAND...: the image is the first argument not starting with -
    fake.write_text('#!/bin/sh\nshift\nfor a; do case "$a" in -*) ;; *) echo "IMAGE=$a"; exit 0;; esac; done\n')
    fake.chmod(0o755)
    return tmp_path


def _run_on_host(host, image, allowed_image_dir=None):
    allowed_image_dir = allowed_image_dir or str(host / "images")
    command = ContainerCommandBuilder("singularity", allowed_image_dir) \
        .with_image(image).with_command(["true"]).with_env("A", "b").get_run_command()
    env = {**os.environ, "PATH": f"{host / 'bin'}:{os.environ['PATH']}"}
    return subprocess.run(["sh", "-c", command], capture_output=True, text=True, env=env, timeout=10)


@pytest.mark.parametrize("name, resolved", [("x.sif", "x.sif"), ("sub/y.sif", "sub/y.sif"), ("inside.sif", "x.sif")])
def test_image_below_the_directory_runs_by_its_resolved_path(host, name, resolved):
    result = _run_on_host(host, str(host / "images" / name))
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"IMAGE={host / 'images' / resolved}\n"


@pytest.mark.parametrize("name", ["escape.sif", "sandbox", "missing.sif"])
def test_image_resolving_elsewhere_or_not_a_file_is_refused(host, name):
    result = _run_on_host(host, str(host / "images" / name))
    assert result.returncode != 0
    assert "IMAGE=" not in result.stdout  # singularity was not run
    assert "not a file below container.allowed_image_dir" in result.stderr
    assert str(host) not in result.stderr  # the path is not echoed


def test_linked_image_directory_is_resolved_too(host):
    (host / "images-link").symlink_to(host / "images")
    result = _run_on_host(host, str(host / "images-link" / "x.sif"), str(host / "images-link"))
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"IMAGE={host / 'images' / 'x.sif'}\n"


def test_failed_check_stops_the_job_script(host):
    from tesp_api.utils.job_script import job_script
    run = ContainerCommandBuilder("singularity", str(host / "images")) \
        .with_image(str(host / "images" / "escape.sif")).with_command(["true"]).get_run_command()
    env = {**os.environ, "PATH": f"{host / 'bin'}:{os.environ['PATH']}"}
    result = subprocess.run(["sh", "-c", job_script("echo stage-in", run, "echo stage-out")], capture_output=True,
                            text=True, env=env, cwd=host, timeout=10)
    assert result.returncode != 0
    assert "stage-out" not in result.stdout


def test_image_paths_are_quoted(host):
    odd = host / "images" / "a b;$(touch pwned).sif"
    odd.write_text("sif")
    result = _run_on_host(host, str(odd))
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"IMAGE={odd}\n"
    assert not (host / "pwned").exists()
    assert shlex.quote(str(odd)) in ContainerCommandBuilder("singularity", str(host / "images")) \
        .with_image(str(odd)).get_run_command()
