"""Server-side allow-list of S3 output buckets and key prefixes, optionally per user (issue #33)."""
import json
import os
import sys

import pydantic
import pytest
from loguru import logger

from tesp_api.api.error import disallowed_s3_outputs_error, get_response_for_error_model
from tesp_api.config.properties import get_properties
from tesp_api.repository.model.task import TesTask, TesTaskIOType
from tesp_api.service.client.client_s3 import S3Config, find_disallowed_s3_outputs, is_allowed_output, \
    load_s3_config, presign_s3_outputs
from tesp_api.utils.types import S3Url

# The package's `client_s3` attribute is a ClientS3 instance, not the module.
client_s3 = sys.modules["tesp_api.service.client.client_s3"]

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIGNED = dict(endpoint_url="https://s3.example.org", access_key_id="AKIATESTACCESSKEY",
              secret_access_key="test-secret-key")
RESTRICTED = S3Config(**SIGNED, allowed_output_prefixes=("results/shared/", "results/users/{user}/", "scratch/"))
FILE, DIRECTORY = TesTaskIOType.FILE, TesTaskIOType.DIRECTORY


class _Model(pydantic.BaseModel):
    url: S3Url


def _s3(url: str) -> S3Url:
    return _Model(url=url).url


def _task(*output_urls, output_type="FILE"):
    return TesTask(executors=[{"image": "ubuntu", "command": ["true"]}],
                   outputs=[{"path": f"/data/out{i}", "url": url, "type": output_type}
                            for i, url in enumerate(output_urls)])


# --- matching ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url, io_type, user", [
    ("s3://results/shared/a.txt", FILE, None),
    ("s3://results/users/alice/a.txt", FILE, "alice"),
    ("s3://results/users/alice/deep/dir", DIRECTORY, "alice"),
    ("s3://results/users/alice", DIRECTORY, "alice"),  # the POST policy allows results/users/alice/...
    ("s3://scratch/anything", FILE, "bob"),
])
def test_allowed_outputs(url, io_type, user):
    assert is_allowed_output(_s3(url), io_type, user, RESTRICTED)


@pytest.mark.parametrize("url, io_type, user", [
    ("s3://results/other/a.txt", FILE, "alice"),
    ("s3://results/users/bob/a.txt", FILE, "alice"),  # another user's prefix
    ("s3://results/users/alice/a.txt", FILE, None),  # {user} entries need an authenticated user
    ("s3://results/users/alice/a.txt", FILE, ""),
    ("s3://results/users/alice2/a.txt", FILE, "alice"),
    ("s3://results/users/alice/b/a.txt", FILE, "alice/b"),  # a user name with / could reach other prefixes
    ("s3://results/users/alice", FILE, "alice"),  # the prefix itself is not below it
    ("s3://results/shared", DIRECTORY, None),  # results/shared/ is the prefix itself: allowed
    ("s3://scratch-other/a.txt", FILE, None),  # the bucket name must match in full
    ("s3://results/shared/../users/bob/a.txt", FILE, None),
    ("s3://results/shared/./a.txt", FILE, None),
])
def test_disallowed_outputs(url, io_type, user):
    allowed = is_allowed_output(_s3(url), io_type, user, RESTRICTED)
    # The DIRECTORY output s3://results/shared posts below results/shared/, which the list allows.
    assert allowed == (url == "s3://results/shared" and io_type == DIRECTORY)


def test_empty_list_allows_everything():
    assert is_allowed_output(_s3("s3://any/key/../x"), FILE, None, S3Config(**SIGNED))


def test_find_disallowed_s3_outputs_names_fields_only():
    task = _task("s3://results/shared/ok.txt", "http://example.org/upload", "s3://results/users/bob/secret-path.txt")
    assert find_disallowed_s3_outputs(task.outputs, "alice", RESTRICTED) == ["outputs[2].url"]
    assert find_disallowed_s3_outputs(task.outputs, "bob", RESTRICTED) == []
    assert find_disallowed_s3_outputs(None, "bob", RESTRICTED) == []


def test_presign_refuses_disallowed_outputs():
    # Tasks queued before the list changed are checked again when they start.
    confs = [{"container_path": "/data/out", "url": _s3("s3://results/users/bob/x"), "type": FILE}]
    with pytest.raises(ValueError, match="allowed_output_prefixes") as error:
        presign_s3_outputs(confs, RESTRICTED, user="alice")
    assert "bob" not in str(error.value)
    [staged] = presign_s3_outputs(confs, RESTRICTED, user="bob")
    assert staged["url"].startswith("https://s3.example.org/results/users/bob/x?")


def test_presign_skips_config_for_non_s3_outputs(monkeypatch):
    def fail():
        raise AssertionError("S3 settings read for a task without S3 outputs")
    monkeypatch.setattr(client_s3, "get_s3_config", fail)
    confs = [{"container_path": "/data/out", "url": "http://example.org/upload", "type": FILE}]
    assert presign_s3_outputs(confs) == confs


# --- task creation -----------------------------------------------------------------------------------------------

# POST /tasks returns this error before storing the task. The endpoint module itself is not imported: it pulls in
# motor, whose asyncio framework module does not import on Python 3.11.
def test_create_task_error_for_disallowed_outputs(monkeypatch):
    monkeypatch.setattr(client_s3, "get_s3_config", lambda: RESTRICTED)
    task = _task("s3://results/shared/ok.txt", "s3://results/users/bob/secret-path.txt")
    assert disallowed_s3_outputs_error(task.outputs, "bob") is None
    response = get_response_for_error_model(disallowed_s3_outputs_error(task.outputs, "alice"))
    assert response.status_code == 403
    body = json.loads(response.body)
    assert "outputs[1].url" in body["message"]
    assert "secret-path" not in body["message"] and "bob" not in body["message"]


# --- settings ----------------------------------------------------------------------------------------------------

@pytest.fixture
def clean_settings(monkeypatch):
    for name in list(os.environ):
        if name.startswith("TESP_API_S3__"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(REPO_ROOT)
    if os.path.exists(os.path.join(REPO_ROOT, ".secrets.toml")):
        pytest.skip("a local .secrets.toml may override the S3 settings")


def test_default_is_no_restriction(clean_settings):
    assert load_s3_config(get_properties()).allowed_output_prefixes == ()


@pytest.mark.parametrize("value", ['["results/users/{user}/", "scratch"]', "results/users/{user}/, scratch"])
def test_prefixes_from_environment(monkeypatch, clean_settings, value):
    monkeypatch.setenv("TESP_API_S3__ALLOWED_OUTPUT_PREFIXES", value)
    # A bare bucket name means the whole bucket.
    assert load_s3_config(get_properties()).allowed_output_prefixes == ("results/users/{user}/", "scratch/")


@pytest.mark.parametrize("entry", ["/key", "bucket/{username}/", "bucket/{user"])
def test_invalid_entries_are_refused(entry):
    with pytest.raises(ValueError, match="allowed_output_prefixes"):
        S3Config(allowed_output_prefixes=(entry,))


def test_warns_when_signed_without_restriction(monkeypatch):
    messages = []
    handler = logger.add(messages.append, level="WARNING")
    try:
        for config in (S3Config(**SIGNED), RESTRICTED, S3Config()):
            monkeypatch.setattr(client_s3, "load_s3_config", lambda config=config: config)
            client_s3.get_s3_config.cache_clear()
            client_s3.get_s3_config()
    finally:
        logger.remove(handler)
        client_s3.get_s3_config.cache_clear()
    assert len(messages) == 1
    assert "allowed_output_prefixes is empty" in messages[0]
