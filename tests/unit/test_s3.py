"""Unit tests for plain ``s3://bucket/key`` inputs with server-side S3 settings (issue #3)."""
import sys

import pydantic
import pytest
from urllib.parse import parse_qs, urlsplit

from tesp_api.config.properties import get_properties
from tesp_api.repository.model.task import TesTask, TesTaskIOType, find_unsupported_s3_urls
from tesp_api.repository.model.task import TesTaskExecutor
from tesp_api.service.client.client_s3 import S3Config, load_s3_config, presign_get_url, redact_presigned_urls
from tesp_api.utils.container import stage_in_command
from tesp_api.utils.types import S3Url

# tesp_api.service.client exports an instance named client_s3, which hides the module.
client_s3 = sys.modules[S3Config.__module__]

ACCESS_KEY = "AKIATESTACCESSKEY"
SECRET_KEY = "test-secret-key-must-not-leak"

SIGNED = S3Config(
    endpoint_url="https://s3.example.org",
    region="eu-west-1",
    access_key_id=ACCESS_KEY,
    secret_access_key=SECRET_KEY,
    addressing_style="path",
    presign_expiry_seconds=900,
)


class _Model(pydantic.BaseModel):
    url: S3Url


def _s3(url: str) -> S3Url:
    return _Model(url=url).url


# --- URL parsing -------------------------------------------------------------

def test_plain_s3_url_is_bucket_and_key():
    url = _s3("s3://my-bucket/work/ab/cdef/.command.sh")
    assert url.bucket == "my-bucket"
    assert url.key == "work/ab/cdef/.command.sh"
    assert url.is_plain()


@pytest.mark.parametrize("legacy", [
    "s3://user:pass@minio.example.org:9000/bucket/key",
    "s3://minio.example.org:9000/bucket/key",
    "s3://bucket/key?versionId=1",
    "s3://bucket/key#fragment",
    "s3://bucket",
    "s3://bucket/",
])
def test_legacy_or_incomplete_s3_urls_are_not_plain(legacy):
    assert not _s3(legacy).is_plain()


# --- request validation ------------------------------------------------------

def _task(input_url=None, output_url=None) -> TesTask:
    return TesTask(
        executors=[{"image": "alpine", "command": ["true"]}],
        inputs=[{"path": "/data/in", "url": input_url}] if input_url else None,
        outputs=[{"path": "/data/out", "url": output_url, "type": "FILE"}] if output_url else None,
    )


def test_plain_s3_task_has_no_unsupported_urls():
    assert find_unsupported_s3_urls(_task("s3://bucket/in.txt", "s3://bucket/out.txt")) == []


def test_legacy_s3_urls_are_reported_by_field_without_echoing_them():
    task = _task("s3://user:secretpass@host:9000/bucket/in.txt", "s3://user:secretpass@host:9000/bucket/out.txt")
    problems = find_unsupported_s3_urls(task)
    assert problems == ["inputs[0].url", "outputs[0].url"]
    assert "secretpass" not in " ".join(problems)


def test_non_s3_urls_are_ignored():
    assert find_unsupported_s3_urls(_task("http://172.17.0.1:5000/test.txt")) == []


# --- settings ----------------------------------------------------------------

def test_settings_are_read_from_environment(monkeypatch):
    monkeypatch.setenv("TESP_API_S3__ENDPOINT_URL", "https://objectstore.example.org")
    monkeypatch.setenv("TESP_API_S3__REGION", "nl-ams-1")
    monkeypatch.setenv("TESP_API_S3__ACCESS_KEY_ID", ACCESS_KEY)
    monkeypatch.setenv("TESP_API_S3__SECRET_ACCESS_KEY", SECRET_KEY)
    monkeypatch.setenv("TESP_API_S3__ADDRESSING_STYLE", "virtual")
    config = load_s3_config(get_properties())
    assert config == S3Config(
        endpoint_url="https://objectstore.example.org",
        region="nl-ams-1",
        access_key_id=ACCESS_KEY,
        secret_access_key=SECRET_KEY,
        addressing_style="virtual",
        presign_expiry_seconds=3600,
    )


def test_default_settings_are_anonymous():
    config = load_s3_config(get_properties())
    assert config.access_key_id is None and config.secret_access_key is None


def test_secret_is_not_in_config_repr():
    assert SECRET_KEY not in repr(SIGNED)


@pytest.mark.parametrize("bad", [
    {"endpoint_url": "s3.example.org"},
    {"endpoint_url": "ftp://s3.example.org"},
    {"addressing_style": "sideways"},
    {"access_key_id": ACCESS_KEY},
    {"secret_access_key": SECRET_KEY},
    {"presign_expiry_seconds": 0},
])
def test_invalid_settings_are_rejected(bad):
    with pytest.raises(ValueError) as error:
        S3Config(**bad)
    assert SECRET_KEY not in str(error.value)


# --- presigning --------------------------------------------------------------

def test_presigned_url_uses_https_endpoint_path_style():
    signed = presign_get_url(_s3("s3://my-bucket/work/ab/in.txt"), SIGNED)
    parts = urlsplit(signed)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "s3.example.org", "/my-bucket/work/ab/in.txt")
    query = parse_qs(parts.query)
    assert query["X-Amz-Expires"] == ["900"]
    assert query["X-Amz-Credential"][0].startswith(f"{ACCESS_KEY}/")
    assert "/eu-west-1/s3/" in query["X-Amz-Credential"][0]
    assert "X-Amz-Signature" in query
    assert SECRET_KEY not in signed


def test_presigned_url_virtual_host_style():
    config = S3Config(**{**SIGNED.__dict__, "addressing_style": "virtual"})
    parts = urlsplit(presign_get_url(_s3("s3://my-bucket/in.txt"), config))
    assert (parts.netloc, parts.path) == ("my-bucket.s3.example.org", "/in.txt")


def test_anonymous_config_gives_unsigned_url():
    config = S3Config(endpoint_url="http://172.17.0.1:9000")
    assert presign_get_url(_s3("s3://public/data/in.txt"), config) == "http://172.17.0.1:9000/public/data/in.txt"


def test_presign_rejects_legacy_url():
    with pytest.raises(ValueError):
        presign_get_url(_s3("s3://user:pass@host:9000/bucket/key"), SIGNED)


def test_redact_presigned_urls():
    signed = presign_get_url(_s3("s3://my-bucket/in.txt"), SIGNED)
    redacted = redact_presigned_urls(f"curl -f -o in.txt '{signed}' && echo done")
    assert ACCESS_KEY not in redacted
    assert parse_qs(urlsplit(signed).query)["X-Amz-Signature"][0] not in redacted
    assert "https://s3.example.org/my-bucket/in.txt?" in redacted
    assert redacted.endswith("' && echo done")


# --- stage-in ----------------------------------------------------------------

def _stage_in(url: str, io_type=TesTaskIOType.FILE) -> str:
    executor = TesTaskExecutor(image="willdockerhub/curl-wget:latest", command=[], workdir="/downloads")
    input_conf = {"url": _s3(url), "pulsar_path": "/staging/inputs/input_file_0", "type": io_type,
                  "container_path": "/data/in.txt"}
    return stage_in_command(executor, {}, "/staging/inputs", [input_conf], "docker")


def test_stage_in_downloads_s3_input_from_configured_endpoint(monkeypatch):
    monkeypatch.setattr(client_s3, "load_s3_config", lambda *args: SIGNED)
    command = _stage_in("s3://my-bucket/work/ab/in.txt")
    assert "s3://" not in command
    assert "https://s3.example.org/my-bucket/work/ab/in.txt?" in command
    assert SECRET_KEY not in command


def test_stage_in_hides_presigned_urls_from_shell_trace(monkeypatch):
    monkeypatch.setattr(client_s3, "load_s3_config", lambda *args: SIGNED)
    command = _stage_in("s3://my-bucket/in.txt")
    assert command.startswith("{ set +x; } 2>/dev/null && ")
    assert command.endswith(" && set -x")


def test_stage_in_rejects_s3_directory_inputs(monkeypatch):
    monkeypatch.setattr(client_s3, "load_s3_config", lambda *args: SIGNED)
    with pytest.raises(ValueError):
        _stage_in("s3://my-bucket/dir/", TesTaskIOType.DIRECTORY)
