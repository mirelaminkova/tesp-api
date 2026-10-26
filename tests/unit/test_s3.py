"""Unit tests for plain ``s3://bucket/key`` inputs with server-side S3 settings (issue #3)."""
import json
import os
import subprocess

import pydantic
import pytest
from urllib.parse import parse_qs, urlsplit

from tesp_api.config.properties import get_properties
from tesp_api.repository.model.task import TesTask, TesTaskIOType, find_unsupported_s3_urls
from tesp_api.repository.model.task import TesTaskExecutor
from tesp_api.service.client.client_s3 import S3Config, load_s3_config, presign_get_url, presign_s3_inputs, \
    redact_presigned_urls
from tesp_api.utils.container import stage_in_command, untraced
from tesp_api.utils.types import S3Url
from tesp_api.api.error import redact_validation_errors
from tesp_api.config.log_config import logg_configure
from loguru import logger

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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


def test_s3_directory_input_is_reported():
    task = TesTask(executors=[{"image": "alpine", "command": ["true"]}],
                   inputs=[{"path": "/data/dir", "url": "s3://bucket/dir/", "type": "DIRECTORY"}])
    assert find_unsupported_s3_urls(task) == ["inputs[0].url"]


def test_plain_s3_input_is_supported():
    assert find_unsupported_s3_urls(_task("s3://bucket/in.txt")) == []


def test_plain_s3_output_is_supported():
    assert find_unsupported_s3_urls(_task(output_url="s3://bucket/out.txt")) == []


def test_validation_errors_do_not_echo_legacy_secrets():
    with pytest.raises(pydantic.ValidationError) as error:
        _task("s3://AKIA:secret/with-slash@host:9000/bucket/key")
    redacted = redact_validation_errors(error.value.errors())
    assert redacted and all(e["loc"] == ["inputs", 0, "url"] for e in redacted)
    assert "with-slash" not in json.dumps(redacted)  # also checks that the errors are JSON serialisable


def test_legacy_s3_urls_are_reported_by_field_without_echoing_them():
    task = _task("s3://user:secretpass@host:9000/bucket/in.txt", "s3://user:secretpass@host:9000/bucket/out.txt")
    problems = find_unsupported_s3_urls(task)
    assert problems == ["inputs[0].url", "outputs[0].url"]
    assert "secretpass" not in " ".join(problems)


def test_non_s3_urls_are_ignored():
    assert find_unsupported_s3_urls(_task("http://172.17.0.1:5000/test.txt")) == []


# --- settings ----------------------------------------------------------------

@pytest.fixture
def clean_settings(monkeypatch):
    """Settings from the repository's settings.toml only, whatever the developer's environment."""
    for name in list(os.environ):
        if name.startswith("TESP_API_S3__"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(REPO_ROOT)
    if os.path.exists(os.path.join(REPO_ROOT, ".secrets.toml")):
        pytest.skip("a local .secrets.toml may override the S3 settings")


def test_settings_are_read_from_environment(monkeypatch, clean_settings):
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


def test_default_settings_are_anonymous_aws(clean_settings):
    assert load_s3_config(get_properties()) == S3Config()


def test_expiry_from_environment_is_an_int(monkeypatch, clean_settings):
    monkeypatch.setenv("TESP_API_S3__PRESIGN_EXPIRY_SECONDS", "'600'")
    assert load_s3_config(get_properties()).presign_expiry_seconds == 600


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


# --- stage-in ---------------------------------------------------------------

def _input_conf(url, io_type=TesTaskIOType.FILE) -> dict:
    return {"url": url, "pulsar_path": "/staging/inputs/input_file_0", "type": io_type,
            "container_path": "/data/in.txt"}


def _stage_in(input_confs) -> str:
    executor = TesTaskExecutor(image="willdockerhub/curl-wget:latest", command=[], workdir="/downloads")
    return stage_in_command(executor, {}, "/staging/inputs", input_confs, "docker")


def test_presign_s3_inputs_replaces_only_s3_urls():
    confs = [_input_conf(_s3("s3://my-bucket/work/ab/in.txt")), _input_conf("http://172.17.0.1:5000/test.txt")]
    staged = presign_s3_inputs(confs, SIGNED)
    assert staged[0]["presigned"] is True
    assert staged[0]["url"].startswith("https://s3.example.org/my-bucket/work/ab/in.txt?")
    assert staged[1] == confs[1]
    assert isinstance(confs[0]["url"], S3Url)  # input confs are not modified


def test_stage_in_downloads_presigned_url():
    command = _stage_in(presign_s3_inputs([_input_conf(_s3("s3://my-bucket/work/ab/in.txt"))], SIGNED))
    assert "s3://" not in command
    assert "https://s3.example.org/my-bucket/work/ab/in.txt?" in command
    assert SECRET_KEY not in command


def test_presign_s3_inputs_rejects_directories():
    with pytest.raises(ValueError):
        presign_s3_inputs([_input_conf(_s3("s3://my-bucket/dir/"), TesTaskIOType.DIRECTORY)], SIGNED)


def test_presign_s3_inputs_rejects_legacy_urls_of_stored_tasks():
    with pytest.raises(ValueError):
        presign_s3_inputs([_input_conf(_s3("s3://user:pass@host:9000/bucket/key"))], SIGNED)


def test_untraced_command_is_not_in_shell_trace():
    signed = presign_get_url(_s3("s3://my-bucket/in.txt"), SIGNED)
    script = f"set -xe && echo before && {untraced(f'test -n {signed!r}')} && echo after"
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "X-Amz-Signature" not in result.stderr
    assert "set +x" not in result.stderr
    assert "+ echo after" in result.stderr  # tracing is back on


# --- logging -----------------------------------------------------------------

def test_presigning_logs_no_signature_or_credential(capsys):
    logg_configure()
    signed = presign_get_url(_s3("s3://my-bucket/logged.txt"), SIGNED)
    out = capsys.readouterr().out
    assert parse_qs(urlsplit(signed).query)["X-Amz-Signature"][0] not in out
    assert ACCESS_KEY not in out


def test_logged_tracebacks_do_not_show_local_variables(capsys):
    logg_configure()

    def submit(command):
        raise ConnectionError("pulsar unreachable")

    run_command_str = "curl 'https://s3.example.org/b/k?X-Amz-Signature=deadbeef'"
    try:
        submit(run_command_str)
    except ConnectionError:
        logger.opt(exception=True).error("submit failed")
    out = capsys.readouterr().out
    assert "pulsar unreachable" in out
    assert "deadbeef" not in out
