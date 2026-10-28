"""Unit tests for ``TaskLog.outputs`` (issue #31).

The stage-out script reports each uploaded file on stdout after a per-job marker; tesp-api turns those lines into
``tesOutputFileLog`` entries from the task's own output URLs (never presigned ones) and drops them from the stored
stdout. The scripts run with the host's ``sh`` and a ``curl`` stub.
"""
import os
import shlex
import stat
import subprocess
from pathlib import Path

import pydantic
import pytest

from tesp_api.repository.model.task import TesTaskExecutor, TesTaskIOType, TesTaskLog
from tesp_api.repository.task_repository_utils import _valid_output_file_logs
from tesp_api.service.client.client_s3 import S3Config, presign_s3_outputs
from tesp_api.utils.container import output_file_logs, stage_out_command, stage_out_script
from tesp_api.utils.types import S3Url

MARKER = "tesp-api-output-0123456789abcdef"
UPLOAD_URL = "http://172.17.0.1:5000/upload"
SIGNED = S3Config(endpoint_url="https://s3.example.org", region="eu-west-1", access_key_id="AKIATESTACCESSKEY",
                  secret_access_key="test-secret-key-must-not-leak", addressing_style="path",
                  presign_expiry_seconds=900, output_presign_expiry_seconds=7200)


class _Model(pydantic.BaseModel):
    url: S3Url


def _s3(url: str) -> S3Url:
    return _Model(url=url).url


def _conf(url, path, io_type=TesTaskIOType.FILE) -> dict:
    return {"container_path": path, "url": url, "volume_name": "vol-1-data", "type": io_type}


@pytest.fixture
def run_stage_out(tmp_path, monkeypatch):
    """Runs a stage-out script with a ``curl`` that prints a response body without a newline and exits with
    ``$CURL_EXIT``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text('#!/bin/sh\nprintf \'{"status": "ok"}\'\nexit "${CURL_EXIT:-0}"\n')
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def run(confs, curl_exit=0):
        monkeypatch.setenv("CURL_EXIT", str(curl_exit))
        return subprocess.run(["sh", "-c", stage_out_script(confs, MARKER)], capture_output=True, text=True,
                              cwd=tmp_path)

    return run


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "data"
    (root / "results" / "sub dir").mkdir(parents=True)
    (root / "results" / "top.txt").write_text("top\n")
    (root / "results" / "sub dir" / "it's #1?.txt").write_text("nested!\n")
    (root / "results" / "tab\there.txt").write_text("")
    (root / "out file.txt").write_text("single\n")
    return root


def test_file_and_directory_outputs_are_reported(run_stage_out, data):
    confs = [_conf(UPLOAD_URL, str(data / "out file.txt")),
             _conf("http://example.org/store/results/", str(data / "results"), TesTaskIOType.DIRECTORY)]
    result = run_stage_out(confs)
    assert result.returncode == 0, result.stderr

    stdout, logs = output_file_logs(result.stdout, MARKER, confs)
    assert MARKER not in stdout
    results = str(data / "results")
    assert sorted(logs, key=lambda log: log["path"]) == sorted([
        {"url": UPLOAD_URL, "path": str(data / "out file.txt"), "size_bytes": "7"},
        {"url": "http://example.org/store/results/top.txt", "path": f"{results}/top.txt", "size_bytes": "4"},
        {"url": "http://example.org/store/results/sub%20dir/it%27s%20%231%3F.txt",
         "path": f"{results}/sub dir/it's #1?.txt", "size_bytes": "8"},
        {"url": "http://example.org/store/results/tab%09here.txt", "path": f"{results}/tab\there.txt",
         "size_bytes": "0"},
    ], key=lambda log: log["path"])
    # The curl response bodies stay in stdout.
    assert stdout.count('{"status": "ok"}') == 4


def test_s3_outputs_report_the_task_url_not_the_presigned_one(run_stage_out, data):
    confs = [_conf(_s3("s3://bucket/work/out.txt"), str(data / "out file.txt")),
             _conf(_s3("s3://bucket/work/results"), str(data / "results"), TesTaskIOType.DIRECTORY)]
    staged = presign_s3_outputs(confs, SIGNED)
    result = run_stage_out(staged)
    assert result.returncode == 0, result.stderr

    _, logs = output_file_logs(result.stdout, MARKER, confs)
    urls = sorted(log["url"] for log in logs)
    assert urls == ["s3://bucket/work/out.txt", "s3://bucket/work/results/sub%20dir/it%27s%20%231%3F.txt",
                    "s3://bucket/work/results/tab%09here.txt", "s3://bucket/work/results/top.txt"]
    assert not any("Amz" in str(log) or "s3.example.org" in str(log) for log in logs)


def test_missing_and_failed_outputs_are_not_reported(run_stage_out, data, tmp_path):
    confs = [_conf(UPLOAD_URL, str(tmp_path / "missing.txt")), _conf(UPLOAD_URL, str(data / "out file.txt"))]
    result = run_stage_out(confs)
    assert result.returncode == 0, result.stderr
    assert [log["path"] for log in output_file_logs(result.stdout, MARKER, confs)[1]] == [str(data / "out file.txt")]

    result = run_stage_out(confs, curl_exit=22)
    assert result.returncode != 0
    assert output_file_logs(result.stdout, MARKER, confs)[1] == []


def test_file_name_with_newline_is_uploaded_but_not_reported(run_stage_out, data):
    (data / "results" / "new\nline.txt").write_text("x")
    confs = [_conf(UPLOAD_URL, str(data / "results"), TesTaskIOType.DIRECTORY)]
    result = run_stage_out(confs)
    assert result.returncode == 0, result.stderr
    paths = [log["path"] for log in output_file_logs(result.stdout, MARKER, confs)[1]]
    assert len(paths) == 3
    assert not any("new" in os.path.basename(path) for path in paths)


def test_no_marker_no_report(run_stage_out, data):
    confs = [_conf(UPLOAD_URL, str(data / "out file.txt"))]
    assert stage_out_script(confs) == stage_out_script(confs, None)
    assert "printf" not in stage_out_script(confs)


def test_lines_without_the_job_marker_are_kept_and_ignored():
    confs = [_conf(UPLOAD_URL, "/data/out.txt")]
    forged = "tesp-api-output-ffffffffffffffff\t0\t1\t\n"
    stdout = f"executor says hi\n{forged}{MARKER}\t0\t12\t\n"
    rest, logs = output_file_logs(stdout, MARKER, confs)
    assert rest == f"executor says hi\n{forged}"
    assert logs == [{"url": UPLOAD_URL, "path": "/data/out.txt", "size_bytes": "12"}]


@pytest.mark.parametrize("line", [
    f"{MARKER}\t1\t12\t\n",        # no such output
    f"{MARKER}\t0\t12\n",          # cut off
    f"{MARKER}\tx\t12\t\n",
    f"{MARKER}\t0\t\t\n",
])
def test_malformed_lines_are_dropped(line):
    rest, logs = output_file_logs(f"before\n{line}after\n", MARKER, [_conf(UPLOAD_URL, "/data/out.txt")])
    assert rest == "before\nafter\n"
    assert logs == []


def test_stage_out_command_carries_the_marker():
    executor = TesTaskExecutor(image="curl-image", command=[], workdir=Path("/downloads"))
    confs = [_conf(UPLOAD_URL, "/data/out.txt")]
    for container_type in ("docker", "singularity"):
        command = stage_out_command(executor, {}, confs, [], container_type, bind_mount="/staging",
                                    output_log_marker=MARKER)
        assert shlex.split(command)[-1] == stage_out_script(confs, MARKER)


def test_reported_logs_are_valid_task_logs():
    confs = [_conf(_s3("s3://bucket/work/results"), "/data/results", TesTaskIOType.DIRECTORY),
             _conf("ftp://ftp.example.org/out.txt", "/data/out.txt")]
    stdout = f"{MARKER}\t0\t3\tsub dir/a #1.txt\n{MARKER}\t1\t5\t\n"
    _, logs = output_file_logs(stdout, MARKER, confs)
    valid = _valid_output_file_logs("task-id", logs)
    assert len(valid) == 2
    task_log = TesTaskLog(logs=[], outputs=valid)
    # Stored and read back as the task document is.
    assert TesTaskLog(**task_log.dict()).outputs[0].url == "s3://bucket/work/results/sub%20dir/a%20%231.txt"


def test_invalid_output_file_logs_are_skipped():
    logs = [{"url": "not a url", "path": "/data/x", "size_bytes": "1"},
            {"url": UPLOAD_URL, "path": "/data/out.txt", "size_bytes": "1"}]
    assert [log.path for log in _valid_output_file_logs("task-id", logs)] == [Path("/data/out.txt")]
