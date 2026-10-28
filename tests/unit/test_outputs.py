"""Unit tests for task outputs over HTTP and plain ``s3://bucket/key`` URLs (issue #4)."""
import base64
import datetime
import inspect
import json
import os
import shlex
import stat
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

import pydantic
import pytest

from tesp_api.config.properties import get_properties
from tesp_api.repository.model.task import TesTask, TesTaskExecutor, TesTaskIOType, find_unsupported_s3_urls
from tesp_api.service.client.client_s3 import S3Config, load_s3_config, presign_get_url, presign_put_url, \
    presign_post_policy, presign_s3_outputs, redact_presigned_urls
from tesp_api.utils.container import stage_out_command, stage_out_script
from tesp_api.utils.types import S3Url

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ACCESS_KEY = "AKIATESTACCESSKEY"
SECRET_KEY = "test-secret-key-must-not-leak"
UPLOAD_URL = "http://172.17.0.1:5000/upload"

SIGNED = S3Config(
    endpoint_url="https://s3.example.org",
    region="eu-west-1",
    access_key_id=ACCESS_KEY,
    secret_access_key=SECRET_KEY,
    addressing_style="path",
    presign_expiry_seconds=900,
    output_presign_expiry_seconds=7200,
)


class _Model(pydantic.BaseModel):
    url: S3Url


def _s3(url: str) -> S3Url:
    return _Model(url=url).url


def _output_conf(url, path="/data/out.txt", io_type=TesTaskIOType.FILE) -> dict:
    return {"container_path": path, "url": url, "volume_name": "vol-1-data", "type": io_type}


# --- request validation ------------------------------------------------------

def _task(outputs) -> TesTask:
    return TesTask(executors=[{"image": "alpine", "command": ["true"]}], outputs=outputs)


@pytest.mark.parametrize("io_type", ["FILE", "DIRECTORY"])
def test_plain_s3_outputs_are_supported(io_type):
    task = _task([{"path": "/data/out", "url": "s3://bucket/work/out", "type": io_type}])
    assert find_unsupported_s3_urls(task) == []


def test_legacy_s3_output_is_still_reported():
    task = _task([{"path": "/data/out", "url": "s3://user:secretpass@host:9000/bucket/out", "type": "FILE"}])
    assert find_unsupported_s3_urls(task) == ["outputs[0].url"]


# --- settings ----------------------------------------------------------------

@pytest.fixture
def clean_settings(monkeypatch):
    for name in list(os.environ):
        if name.startswith("TESP_API_S3__"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(REPO_ROOT)
    if os.path.exists(os.path.join(REPO_ROOT, ".secrets.toml")):
        pytest.skip("a local .secrets.toml may override the S3 settings")


def test_output_expiry_defaults_to_one_day(clean_settings):
    assert load_s3_config(get_properties()).output_presign_expiry_seconds == 24 * 3600


def test_output_expiry_from_environment(monkeypatch, clean_settings):
    monkeypatch.setenv("TESP_API_S3__OUTPUT_PRESIGN_EXPIRY_SECONDS", "'172800'")
    assert load_s3_config(get_properties()).output_presign_expiry_seconds == 172800


@pytest.mark.parametrize("seconds", [0, 7 * 24 * 3600 + 1])
def test_invalid_output_expiry_is_rejected(seconds):
    with pytest.raises(ValueError):
        S3Config(output_presign_expiry_seconds=seconds)


# --- presigning: FILE outputs (PUT) -------------------------------------------

def test_presigned_put_url():
    signed = presign_put_url(_s3("s3://my-bucket/work/ab/out.txt"), SIGNED)
    parts = urlsplit(signed)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "s3.example.org", "/my-bucket/work/ab/out.txt")
    query = parse_qs(parts.query)
    assert query["X-Amz-Expires"] == ["7200"]  # output lifetime, not the input one
    assert query["X-Amz-Credential"][0].startswith(f"{ACCESS_KEY}/")
    assert SECRET_KEY not in signed


def test_presigned_put_url_is_not_a_get_url():
    url = _s3("s3://my-bucket/out.txt")
    put_signature = parse_qs(urlsplit(presign_put_url(url, SIGNED)).query)["X-Amz-Signature"]
    config = S3Config(**{**SIGNED.__dict__, "presign_expiry_seconds": 7200})
    get_signature = parse_qs(urlsplit(presign_get_url(url, config)).query)["X-Amz-Signature"]
    assert put_signature != get_signature


def test_anonymous_put_url_is_plain():
    config = S3Config(endpoint_url="http://172.17.0.1:9000")
    assert presign_put_url(_s3("s3://public/out.txt"), config) == "http://172.17.0.1:9000/public/out.txt"


def test_presign_put_rejects_legacy_url():
    with pytest.raises(ValueError):
        presign_put_url(_s3("s3://user:pass@host:9000/bucket/key"), SIGNED)


# --- presigning: DIRECTORY outputs (POST policy) --------------------------------

def _policy(post: dict) -> dict:
    return json.loads(base64.b64decode(post["fields"]["policy"]))


@pytest.mark.parametrize("url", ["s3://my-bucket/work/ab/results", "s3://my-bucket/work/ab/results/"])
def test_post_policy_is_limited_to_the_key_prefix(url):
    before = datetime.datetime.now(datetime.timezone.utc)
    post = presign_post_policy(_s3(url), SIGNED)
    assert post["url"] == "https://s3.example.org/my-bucket"
    assert post["key_prefix"] == "work/ab/results/"
    policy = _policy(post)
    assert ["starts-with", "$key", "work/ab/results/"] in policy["conditions"]
    assert {"bucket": "my-bucket"} in policy["conditions"]
    # No condition may allow any other key.
    key_conditions = [c for c in policy["conditions"] if "$key" in c or (isinstance(c, dict) and "key" in c)]
    assert key_conditions and all(c == ["starts-with", "$key", "work/ab/results/"] for c in key_conditions)
    expiration = datetime.datetime.strptime(policy["expiration"], "%Y-%m-%dT%H:%M:%SZ") \
        .replace(tzinfo=datetime.timezone.utc)
    assert datetime.timedelta(seconds=7100) < expiration - before <= datetime.timedelta(seconds=7201)


def test_post_policy_fields_carry_signature_but_no_secret():
    post = presign_post_policy(_s3("s3://my-bucket/results"), SIGNED)
    fields = post["fields"]
    assert "key" not in fields  # set per file by the stage-out script
    assert fields["x-amz-credential"].startswith(f"{ACCESS_KEY}/")
    assert fields["x-amz-signature"]
    assert SECRET_KEY not in json.dumps(post)


def test_post_policy_rejects_legacy_url():
    with pytest.raises(ValueError):
        presign_post_policy(_s3("s3://user:pass@host:9000/bucket/dir"), SIGNED)


# --- presign_s3_outputs -----------------------------------------------------------

def test_presign_s3_outputs_replaces_only_s3_urls():
    confs = [
        _output_conf(_s3("s3://my-bucket/work/out.txt")),
        _output_conf(_s3("s3://my-bucket/work/results"), "/data/results", TesTaskIOType.DIRECTORY),
        _output_conf(UPLOAD_URL),
    ]
    staged = presign_s3_outputs(confs, SIGNED)

    assert staged[0]["presigned"] is True
    assert staged[0]["upload"] == "s3_put"
    assert staged[0]["url"].startswith("https://s3.example.org/my-bucket/work/out.txt?")

    assert staged[1]["presigned"] is True
    assert staged[1]["upload"] == "s3_post"
    assert staged[1]["url"] == "https://s3.example.org/my-bucket"
    assert staged[1]["key_prefix"] == "work/results/"
    assert "x-amz-signature" in staged[1]["form_fields"]

    assert staged[2] == confs[2]
    assert isinstance(confs[0]["url"], S3Url)  # output confs are not modified
    assert "presigned" not in confs[1]


def test_presign_s3_outputs_rejects_legacy_urls_of_stored_tasks():
    with pytest.raises(ValueError):
        presign_s3_outputs([_output_conf(_s3("s3://user:pass@host:9000/bucket/key"))], SIGNED)


def test_anonymous_post_policy_has_no_fields():
    config = S3Config(endpoint_url="http://172.17.0.1:9000")
    post = presign_post_policy(_s3("s3://public/results"), config)
    assert post == {"url": "http://172.17.0.1:9000/public", "fields": {}, "key_prefix": "results/"}


def test_redact_presigned_post_fields():
    staged = presign_s3_outputs(
        [_output_conf(_s3("s3://my-bucket/results"), "/data/results", TesTaskIOType.DIRECTORY)], SIGNED)
    script = stage_out_script(staged)
    signature = staged[0]["form_fields"]["x-amz-signature"]
    assert signature in script
    redacted = redact_presigned_urls(script)
    assert signature not in redacted
    assert ACCESS_KEY not in redacted
    # The whole field goes, its name too, so a log scan for the names finds only unredacted ones.
    assert "x-amz-credential" not in redacted and "policy=" not in redacted


# --- stage-out script ---------------------------------------------------------------

def _curl_unquote(value: str):
    """Parses a double-quoted ``curl -F`` file name (backslash escapes ``\\`` and ``"``); returns it and the rest."""
    assert value.startswith('"'), value
    name, i = "", 1
    while value[i] != '"':
        if value[i] == "\\":
            i += 1
        name += value[i]
        i += 1
    return name, value[i + 1:]


FAKE_CURL = """#!{python}
import json, os, sys
{unquote}
args = sys.argv[1:]
files = {{}}
for i, arg in enumerate(args):
    if arg in ("-T", "--upload-file"):
        files[args[i + 1]] = open(args[i + 1]).read()
    if arg in ("-F", "--form") and "=@" in args[i + 1]:
        path, _ = _curl_unquote(args[i + 1].split("=@", 1)[1])
        files[path] = open(path).read()
with open(os.environ["FAKE_CURL_LOG"], "a") as log:
    log.write(json.dumps({{"args": args, "cwd": os.getcwd(), "files": files}}) + "\\n")
sys.exit(int(os.environ.get("FAKE_CURL_EXIT", "0")))
"""


@pytest.fixture
def fake_curl(tmp_path, monkeypatch):
    """Runs a stage-out script with a ``curl`` that records its calls instead of uploading."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(FAKE_CURL.format(python=sys.executable, unquote=inspect.getsource(_curl_unquote)))
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "curl.log"
    monkeypatch.setenv("FAKE_CURL_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def run(confs, exit_code=0):
        monkeypatch.setenv("FAKE_CURL_EXIT", str(exit_code))
        result = subprocess.run(["sh", "-c", stage_out_script(confs)], capture_output=True, text=True,
                                cwd=tmp_path)
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        log.unlink(missing_ok=True)
        return result, calls

    return run


@pytest.fixture
def outputs_dir(tmp_path):
    """A directory output with nested files, including names that need quoting."""
    root = tmp_path / "data"
    (root / "results" / "sub dir").mkdir(parents=True)
    (root / "results" / "top.txt").write_text("top\n")
    (root / "results" / "sub dir" / "it's nested.txt").write_text("nested\n")
    # curl -F parses ; and , after a file name unless it is quoted, with " and \ escaped.
    (root / "results" / 'a;type=x,y "q" \\b.txt').write_text("special\n")
    (root / "out file.txt").write_text("single\n")
    return root


def _form(call) -> list:
    args = call["args"]
    return [args[i + 1] for i, arg in enumerate(args) if arg in ("-F", "--form", "--form-string")]


def test_http_file_output_is_posted(fake_curl, outputs_dir):
    path = str(outputs_dir / "out file.txt")
    result, calls = fake_curl([_output_conf(UPLOAD_URL, path)])
    assert result.returncode == 0, result.stderr
    assert len(calls) == 1
    assert calls[0]["args"][-1] == UPLOAD_URL
    assert calls[0]["files"] == {path: "single\n"}


def test_http_directory_output_keeps_structure(fake_curl, outputs_dir):
    path = str(outputs_dir / "results")
    result, calls = fake_curl([_output_conf(UPLOAD_URL, path, TesTaskIOType.DIRECTORY)])
    assert result.returncode == 0, result.stderr
    names = []
    for call in calls:
        [form] = _form(call)
        path, rest = _curl_unquote(form[len("file=@"):])
        assert rest.startswith(";filename=")
        name, rest = _curl_unquote(rest[len(";filename="):])
        assert (path, rest) == (name, "")
        names.append(name)
    assert sorted(names) == ['a;type=x,y "q" \\b.txt', "sub dir/it's nested.txt", "top.txt"]
    assert sorted(content for call in calls for content in call["files"].values()) == \
        ["nested\n", "special\n", "top\n"]
    assert all(call["args"][-1] == UPLOAD_URL for call in calls)


def test_s3_file_output_is_put(fake_curl, outputs_dir):
    path = str(outputs_dir / "out file.txt")
    staged = presign_s3_outputs([_output_conf(_s3("s3://my-bucket/work/out.txt"), path)], SIGNED)
    result, calls = fake_curl(staged)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 1
    args = calls[0]["args"]
    assert args[args.index("-T") + 1] == path
    assert args[-1] == staged[0]["url"]
    assert calls[0]["files"] == {path: "single\n"}


def test_s3_directory_output_posts_each_file_under_the_prefix(fake_curl, outputs_dir):
    path = str(outputs_dir / "results")
    staged = presign_s3_outputs(
        [_output_conf(_s3("s3://my-bucket/work/results"), path, TesTaskIOType.DIRECTORY)], SIGNED)
    result, calls = fake_curl(staged)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 3
    keys = []
    for call in calls:
        args = call["args"]
        assert args[-1] == "https://s3.example.org/my-bucket"
        fields = dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "--form-string")
        assert {k: v for k, v in fields.items() if k != "key"} == staged[0]["form_fields"]
        keys.append(fields["key"])
        # S3 ignores form fields after the file, so the file goes last.
        assert args[-3] == "-F" and args[-2].startswith("file=@")
    assert sorted(keys) == ['work/results/a;type=x,y "q" \\b.txt', "work/results/sub dir/it's nested.txt",
                           "work/results/top.txt"]
    assert sorted(content for call in calls for content in call["files"].values()) == \
        ["nested\n", "special\n", "top\n"]


def test_missing_outputs_are_skipped(fake_curl, tmp_path):
    staged = presign_s3_outputs([
        _output_conf(_s3("s3://my-bucket/work/missing.txt"), str(tmp_path / "missing.txt")),
        _output_conf(_s3("s3://my-bucket/work/missing"), str(tmp_path / "missing"), TesTaskIOType.DIRECTORY),
        _output_conf(UPLOAD_URL, str(tmp_path / "missing-http.txt")),
        _output_conf(UPLOAD_URL, str(tmp_path / "missing-http"), TesTaskIOType.DIRECTORY),
    ], SIGNED)
    result, calls = fake_curl(staged)
    assert result.returncode == 0, result.stderr
    assert calls == []
    assert result.stderr.count("not found, skipped") == 4


@pytest.mark.parametrize("io_type", [TesTaskIOType.FILE, TesTaskIOType.DIRECTORY])
def test_output_of_the_wrong_type_fails_the_stage_out(fake_curl, outputs_dir, io_type):
    path = str(outputs_dir / ("results" if io_type == TesTaskIOType.FILE else "out file.txt"))
    result, _ = fake_curl([_output_conf(UPLOAD_URL, path, io_type)])
    assert result.returncode != 0
    assert "skipped" not in result.stderr


def test_missing_output_does_not_stop_later_outputs(fake_curl, outputs_dir, tmp_path):
    present = str(outputs_dir / "out file.txt")
    result, calls = fake_curl([_output_conf(UPLOAD_URL, str(tmp_path / "missing.txt")),
                               _output_conf(UPLOAD_URL, present)])
    assert result.returncode == 0, result.stderr
    assert [list(call["files"]) for call in calls] == [[present]]


def test_empty_directory_output_uploads_nothing(fake_curl, tmp_path):
    (tmp_path / "empty").mkdir()
    result, calls = fake_curl([_output_conf(UPLOAD_URL, str(tmp_path / "empty"), TesTaskIOType.DIRECTORY)])
    assert result.returncode == 0, result.stderr
    assert calls == []


@pytest.mark.parametrize("io_type", [TesTaskIOType.FILE, TesTaskIOType.DIRECTORY])
def test_failed_upload_fails_the_stage_out(fake_curl, outputs_dir, io_type):
    path = str(outputs_dir / ("out file.txt" if io_type == TesTaskIOType.FILE else "results"))
    result, _ = fake_curl([_output_conf(UPLOAD_URL, path, io_type)], exit_code=22)
    assert result.returncode != 0


def test_stage_out_command_contains_no_s3_url_or_secret():
    staged = presign_s3_outputs([
        _output_conf(_s3("s3://my-bucket/work/out.txt")),
        _output_conf(_s3("s3://my-bucket/work/results"), "/data/results", TesTaskIOType.DIRECTORY),
    ], SIGNED)
    executor = TesTaskExecutor(image="willdockerhub/curl-wget:latest", command=[], workdir="/downloads")
    volume_confs = [{"volume_name": "vol-1-data", "container_path": "/data"}]
    for container_type in ("docker", "singularity"):
        command = stage_out_command(executor, {}, staged, volume_confs, container_type, bind_mount="/staging")
        assert "s3://" not in command
        assert SECRET_KEY not in command
        assert "https://s3.example.org/my-bucket/work/out.txt?" in command
        # The script survives the extra quoting of the container command line; printf feeds it to `sh -s`.
        tokens = shlex.split(command)
        start = tokens.index("printf")  # singularity creates the bind mount directory first
        assert tokens[start:start + 4] == ["printf", "%s\\n", "{", stage_out_script(staged)]
        assert tokens[-2:] == ["sh", "-s"]


# --- review round: validation and robustness ------------------------------------

from tesp_api.repository.model.task import find_unsupported_outputs  # noqa: E402


@pytest.mark.parametrize("output, field", [
    ({"path": "/data/*.bam", "url": UPLOAD_URL, "type": "FILE"}, "outputs[0].path"),
    ({"path": "/data/sample?.txt", "url": UPLOAD_URL, "type": "FILE"}, "outputs[0].path"),
    ({"path": "data/out.txt", "url": UPLOAD_URL, "type": "FILE"}, "outputs[0].path"),
])
def test_unsupported_output_paths_are_reported(output, field):
    assert find_unsupported_outputs(_task([output])) == [field]


def test_plain_output_paths_are_supported():
    assert find_unsupported_outputs(_task([
        {"path": "/data/out file.txt", "url": UPLOAD_URL, "type": "FILE"},
        {"path": "/data/sample[1].txt", "url": UPLOAD_URL, "type": "FILE"},
        # TES 1.1: path_prefix is ignored when the path has no wildcards.
        {"path": "/data/out", "path_prefix": "/data", "url": UPLOAD_URL, "type": "DIRECTORY"},
        {"path": "/data/results", "url": "s3://bucket/results", "type": "DIRECTORY"},
    ])) == []


def test_s3_directory_output_key_with_filename_variable_is_reported():
    # S3 POST replaces ${filename} in the key field.
    task = _task([{"path": "/data/out", "url": "s3://bucket/a${filename}", "type": "DIRECTORY"}])
    assert find_unsupported_s3_urls(task) == ["outputs[0].url"]


def test_redaction_keeps_other_parameters_ending_in_policy():
    assert redact_presigned_urls("retention-policy=keep") == "retention-policy=keep"


def test_redaction_hides_policy_which_contains_the_access_key_id():
    staged = presign_s3_outputs(
        [_output_conf(_s3("s3://my-bucket/results"), "/data/results", TesTaskIOType.DIRECTORY)], SIGNED)
    redacted = redact_presigned_urls(stage_out_script(staged))
    assert staged[0]["form_fields"]["policy"] not in redacted
    assert "policy=" not in redacted  # the whole field goes, name included (issue #39)


def test_directory_output_with_many_files_uploads_all_in_batches(fake_curl, tmp_path):
    results = tmp_path / "many"
    results.mkdir()
    for i in range(250):
        (results / f"f{i:03}.txt").write_text(f"{i}\n")
    result, calls = fake_curl([_output_conf(UPLOAD_URL, str(results), TesTaskIOType.DIRECTORY)])
    assert result.returncode == 0, result.stderr
    assert len(calls) == 250


def test_file_name_ending_in_newline(fake_curl, tmp_path):
    results = tmp_path / "nl"
    results.mkdir()
    (results / "name\n").write_text("nl\n")
    result, calls = fake_curl([_output_conf(UPLOAD_URL, str(results), TesTaskIOType.DIRECTORY)])
    assert result.returncode == 0, result.stderr
    assert [list(call["files"].values()) for call in calls] == [["nl\n"]]


def test_directory_output_does_not_follow_symbolic_links(fake_curl, tmp_path):
    results = tmp_path / "links"
    results.mkdir()
    (results / "real.txt").write_text("real\n")
    (results / "loop").symlink_to(".")
    (results / "outside").symlink_to("/etc/hostname")
    result, calls = fake_curl([_output_conf(UPLOAD_URL, str(results), TesTaskIOType.DIRECTORY)])
    assert result.returncode == 0, result.stderr
    assert [list(call["files"].values()) for call in calls] == [["real\n"]]
    assert f"tesp-api: {results}/loop is a symbolic link, skipped" in result.stderr.splitlines()
    assert result.stderr.count("is a symbolic link, skipped") == 2


def test_output_path_is_not_interpreted_by_the_shell(fake_curl, tmp_path):
    results = tmp_path / 'odd $(touch pwned) "dir"'
    results.mkdir()
    (results / "link").symlink_to(".")
    (results / "a.txt").write_text("a\n")
    result, calls = fake_curl([_output_conf(UPLOAD_URL, str(results), TesTaskIOType.DIRECTORY)])
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "pwned").exists() and not (results / "pwned").exists()
    assert f"tesp-api: {results}/link is a symbolic link, skipped" in result.stderr.splitlines()
    assert len(calls) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any directory")
def test_unreadable_directory_fails_the_stage_out(fake_curl, tmp_path):
    results = tmp_path / "locked"
    (results / "sub").mkdir(parents=True)
    (results / "ok.txt").write_text("ok\n")
    (results / "sub").chmod(0)
    try:
        result, _ = fake_curl([_output_conf(UPLOAD_URL, str(results), TesTaskIOType.DIRECTORY)])
    finally:
        (results / "sub").chmod(0o755)
    assert result.returncode != 0


def test_curl_does_not_glob_task_urls(fake_curl, outputs_dir):
    url = "http://172.17.0.1:5000/upload/{a,b}/[1-2]"
    result, calls = fake_curl([_output_conf(url, str(outputs_dir / "out file.txt")),
                               _output_conf(url, str(outputs_dir / "results"), TesTaskIOType.DIRECTORY)])
    assert result.returncode == 0, result.stderr
    assert calls and all(call["args"][0] == "-fsSg" and call["args"][-1] == url for call in calls)


def test_s3_directory_file_named_like_the_filename_variable_fails(fake_curl, tmp_path):
    results = tmp_path / "vars"
    results.mkdir()
    (results / "a${filename}").write_text("x\n")
    staged = presign_s3_outputs(
        [_output_conf(_s3("s3://my-bucket/work/results"), str(results), TesTaskIOType.DIRECTORY)], SIGNED)
    result, calls = fake_curl(staged)
    assert result.returncode != 0
    assert calls == []
    assert "tesp-api: cannot upload a${filename} to S3: its name contains ${filename}" in result.stderr.splitlines()
