"""Presigned S3 URLs and POST policies must not reach tesp-api's log or exception messages (issue #39)."""
import asyncio
import os
import subprocess

import aiohttp
import pydantic
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from loguru import logger
from yarl import URL

from tesp_api.config.log_config import logg_configure
from tesp_api.repository.model.task import TesTaskIOType
from tesp_api.service.client.client_s3 import S3Config, presign_get_url, presign_s3_outputs, redact_presigned_urls
from tesp_api.service.pulsar_operations import PulsarLayerConnectionError, PulsarOperationsError, \
    PulsarRestOperations, log_pulsar_request
from tesp_api.utils.container import stage_out_script
from tesp_api.utils.types import S3Url

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCAN_LOGS = os.path.join(REPO_ROOT, "ci", "scan-logs.sh")
ACCESS_KEY = "AKIATESTACCESSKEY"
SECRET_KEY = "test-secret-key-must-not-leak"

SIGNED = S3Config(
    endpoint_url="https://s3.example.org",
    region="eu-west-1",
    access_key_id=ACCESS_KEY,
    secret_access_key=SECRET_KEY,
    addressing_style="path",
)

# What the CI log scan (ci/scan-logs.sh) looks for, lower case.
MARKERS = ("x-amz-signature", "x-amz-credential", "x-amz-security-token", "policy=", "policy%3d")


class _Model(pydantic.BaseModel):
    url: S3Url


def _s3(url: str) -> S3Url:
    return _Model(url=url).url


def _command_line() -> str:
    """A job command line as tesp-api submits it: a presigned GET for an input, a presigned POST for an output."""
    get_url = presign_get_url(_s3("s3://my-bucket/work/in.txt"), SIGNED)
    staged = presign_s3_outputs([{"container_path": "/data/results", "url": _s3("s3://my-bucket/work/results"),
                                  "volume_name": "vol-1-data", "type": TesTaskIOType.DIRECTORY}], SIGNED)
    return f"set -xe && curl -fsSg -o /data/in.txt '{get_url}' && {stage_out_script(staged)}"


def _assert_clean(text: str):
    assert ACCESS_KEY not in text
    assert SECRET_KEY not in text
    for marker in MARKERS:
        assert marker not in text.lower(), marker


# --- redact_presigned_urls ------------------------------------------------------

def test_command_line_carries_presigned_secrets():
    # Guards the tests below: without redaction they would find every marker.
    command = _command_line().lower()
    assert ACCESS_KEY.lower() in command
    assert all(marker in command for marker in ("x-amz-signature", "x-amz-credential", "policy="))


def test_redacts_get_url_and_post_fields():
    command = _command_line()
    redacted = redact_presigned_urls(command)
    _assert_clean(redacted)
    assert "https://s3.example.org/my-bucket/work/in.txt?" in redacted
    assert "X-Amz-Expires=" in redacted  # parameters that are not secret stay
    assert redacted.startswith("set -xe && curl -fsSg -o /data/in.txt '")


def test_redacts_url_encoded_command_line():
    # How aiohttp/yarl encode the command line into Pulsar's `command_line` query parameter.
    url = URL("http://pulsar:8913/jobs/abc/submit").with_query({"command_line": _command_line()})
    assert "%3D" in str(url) and ACCESS_KEY in str(url)
    redacted = redact_presigned_urls(str(url))
    _assert_clean(redacted)
    assert redacted.startswith("http://pulsar:8913/jobs/abc/submit?command_line=set+-xe")


TOKENS_AND_POLICIES = [
    "X-Amz-Security-Token=FQoGZXIvYXdzE%2Fabc&X-Amz-Date=1",
    "x-amz-security-token%3DFQoGZXIvYXdzE%252Fabc%26x",
    "--form-string 'policy=eyJleHBpcmF0aW9uIjoi+/a==' -F",
    "+--form-string+%27policy%3DeyJleHBpcmF0aW9uIjoi%2B/a%3D%3D%27+-F",
]


@pytest.mark.parametrize("text", TOKENS_AND_POLICIES)
def test_redacts_session_tokens_and_policies(text):
    _assert_clean(redact_presigned_urls(text))


def test_keeps_unrelated_text():
    text = "restart_policy=always X-Amz-Date=20261026T000000Z key=work/results/"
    assert redact_presigned_urls(text) == text


# --- Pulsar request logging ------------------------------------------------------

async def _with_pulsar_stub(handler, scenario):
    """Runs ``scenario(operations, received)`` against a stub Pulsar whose /jobs/{id}/submit is ``handler``."""
    received = []

    async def submit(request):
        received.append(request.query.get("command_line"))
        return await handler(request)

    app = web.Application()
    app.router.add_post("/jobs/{job_id}/submit", submit)
    server = TestServer(app)
    await server.start_server(access_log=None)  # the stub's access log is Pulsar's, not tesp-api's
    try:
        trace_config = aiohttp.TraceConfig()
        trace_config.on_request_start.append(log_pulsar_request)
        async with aiohttp.ClientSession(trace_configs=[trace_config]) as session:
            operations = PulsarRestOperations(session, str(server.make_url("")).rstrip("/"), 0, 1)
            await scenario(operations, received)
    finally:
        await server.close()
    return received


def test_request_log_has_path_but_no_command_line(capsys):
    logg_configure()
    command = _command_line()

    async def ok(request):
        return web.Response(body=b"")

    async def scenario(operations, received):
        await operations.run_job("abc", command)

    received = asyncio.run(_with_pulsar_stub(ok, scenario))
    assert received == [command]  # Pulsar still gets the whole command line
    out = capsys.readouterr().out
    assert "/jobs/abc/submit" in out
    assert "POST" in out
    assert "command_line" not in out
    _assert_clean(out)


def test_pulsar_error_messages_and_tracebacks_are_redacted(capsys):
    logg_configure()
    command = _command_line()

    async def not_json(request):
        return web.Response(text="Internal Server Error", content_type="text/plain")

    errors = []

    async def scenario(operations, received):
        # A ClientResponseError (here ContentTypeError) names the request URL, query included. No public operation
        # sends command_line and expects JSON, so the request is made directly.
        try:
            await operations._pulsar_request(path="/jobs/abc/submit", method="POST", response_type="JSON",
                                             params={"command_line": command})
        except PulsarLayerConnectionError as error:
            errors.append(error)
            logger.opt(exception=True).error(f"Pulsar connection error [msg: {error}]")

    asyncio.run(_with_pulsar_stub(not_json, scenario))
    [error] = errors
    assert "/jobs/abc/submit" in str(error)
    _assert_clean(str(error))
    _assert_clean(repr(error))
    _assert_clean(capsys.readouterr().out)


def test_wrapped_operation_errors_are_redacted(capsys):
    logg_configure()
    leaky = ValueError(f"unexpected answer for {_command_line()}")
    try:
        try:
            raise leaky
        except ValueError as cause:
            PulsarRestOperations._reraise_custom(cause)
    except PulsarOperationsError as error:
        assert "ValueError: unexpected answer for set -xe" in str(error)  # the wrapped error is hidden from tracebacks
        _assert_clean(str(error))
        logger.opt(exception=True).error(f"Pulsar operations error [msg: {error}]")
    _assert_clean(capsys.readouterr().out)


# --- CI log scan -------------------------------------------------------------------

def _scan(tmp_path, text: str) -> subprocess.CompletedProcess:
    log = tmp_path / "compose.log"
    log.write_text(text)
    return subprocess.run(["bash", SCAN_LOGS, str(log)], capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("leak", [
    _command_line(),
    str(URL("http://pulsar:8913/jobs/abc/submit").with_query({"command_line": _command_line()})),
    *TOKENS_AND_POLICIES,
], ids=["raw", "url-encoded", "token", "encoded-token", "policy", "encoded-policy"])
def test_log_scan_fails_on_presigned_urls_without_printing_them(tmp_path, leak):
    result = _scan(tmp_path, f"tesp-api-1  | DEBUG {leak}\n")
    assert result.returncode == 1, result.stderr
    assert "line 1" in result.stdout + result.stderr
    _assert_clean(result.stdout + result.stderr)


def test_log_scan_passes_redacted_logs(tmp_path):
    command = _command_line()
    url = URL("http://pulsar:8913/jobs/abc/submit").with_query({"command_line": command})
    logged = [command, str(url), *TOKENS_AND_POLICIES]
    result = _scan(tmp_path, "".join(f"tesp-api-1  | {redact_presigned_urls(text)}\n" for text in logged))
    assert result.returncode == 0, result.stdout + result.stderr


def test_log_scan_reports_missing_files(tmp_path):
    result = subprocess.run(["bash", SCAN_LOGS, str(tmp_path / "missing.log")], capture_output=True, text=True,
                            timeout=30)
    assert result.returncode == 2


def test_log_scan_reports_leaks_after_an_unreadable_file(tmp_path):
    log = tmp_path / "compose.log"
    log.write_text(f"tesp-api-1  | {_command_line()}\n")
    result = subprocess.run(["bash", SCAN_LOGS, str(tmp_path / "missing.log"), str(log)], capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 2
    assert f"{log} line 1: tesp-api-1" in result.stderr
