"""logs[].outputs (issue #31): the stage-out reports each file it uploaded; tesp-api lists them in the task's last log.

The stage-out's stdout, holding only those reports (the upload responses are dropped), goes to a file in the job's
working directory that tesp-api reads from Pulsar, rather than to the job's stdout, which Pulsar returns only in part.
The tests run the real job script with the host's shell, a ``docker`` stub that runs the container command on the host
and a ``curl`` stub that accepts every upload.
"""
import subprocess
from pathlib import Path

import pytest

from tesp_api.repository.model.task import TesTaskExecutor, TesTaskState, parse_output_file_logs
from tesp_api.utils.container import OUTPUT_REPORT_PREFIX, output_file_logs, stage_out_script
from tesp_api.utils.job_script import EXECUTOR_LOG_TAIL_BYTES, STAGE_OUT_REPORT_FILE, job_script
from tests.unit.run_task_stub import ScriptPulsar, run_task

CURL_STUB = """#!/bin/sh
# Accepts every upload; records the arguments, one call per line.
printf '%s\n' "$*" | tr '\n' ' ' >> "$(dirname "$0")/curl.calls"; echo >> "$(dirname "$0")/curl.calls"
"""

HTTP = "http://example.org/upload"


def _report(*lines) -> str:
    return "".join(f"{OUTPUT_REPORT_PREFIX}\t" + "\t".join(map(str, line)) + "\n" for line in lines)


# --- Parsing the report -----------------------------------------------------------------------------------------------

def test_file_directory_and_wildcard_outputs():
    confs = [
        {"container_path": "/data/out.txt", "url": HTTP, "type": "FILE"},
        {"container_path": "/data/dir", "url": "s3://bucket/prefix/dir", "type": "DIRECTORY"},
        {"container_path": "/data/dir2", "url": HTTP, "type": "DIRECTORY"},
        {"container_path": "/data/w/*.txt", "url": HTTP, "type": "FILE", "path_prefix": "/data/"},
    ]
    report = _report((0, 5, ""), (1, 7, "sub dir/a b.txt"), (2, 3, "sub/c.txt"), (3, 9, "x.txt"))
    assert output_file_logs(report, confs) == [
        {"url": HTTP, "path": "/data/out.txt", "size_bytes": "5"},
        # S3: the file's key is the output's prefix and its relative path
        {"url": "s3://bucket/prefix/dir/sub%20dir/a%20b.txt", "path": "/data/dir/sub dir/a b.txt", "size_bytes": "7"},
        # HTTP(S): every file is POSTed to the output's URL, with its relative path as the multipart file name
        {"url": HTTP, "path": "/data/dir2/sub/c.txt", "size_bytes": "3"},
        {"url": HTTP, "path": "/data/w/x.txt", "size_bytes": "9"},
    ]


def test_lines_that_are_not_reports_are_left_out():
    confs = [{"container_path": "/data/out.txt", "url": HTTP, "type": "FILE"}]
    report = ("<html>an upload response</html>\n"
              f"{OUTPUT_REPORT_PREFIX}\t1\t5\t\n"             # no such output
              f"{OUTPUT_REPORT_PREFIX}\tx\t5\t\n"
              f"{OUTPUT_REPORT_PREFIX}\t0\t\t\n"              # no size
              f"other\t0\t5\t\n"
              f"{OUTPUT_REPORT_PREFIX}\t0\t5")                 # cut off: no name field
    assert output_file_logs(report, confs) == []


def test_invalid_entries_are_left_out_of_the_model():
    valid, invalid = parse_output_file_logs([{"url": HTTP, "path": "/a", "size_bytes": "1"},
                                             {"url": "not a url", "path": "/b", "size_bytes": "1"}])
    assert [str(log.path) for log in valid] == ["/a"] and [log["path"] for log in invalid] == ["/b"]


# --- The stage-out script ---------------------------------------------------------------------------------------------

@pytest.fixture
def stubbed_curl(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "curl").write_text(CURL_STUB)
    (bin_dir / "curl").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    return bin_dir / "curl.calls"


@pytest.mark.parametrize("shell", ["bash", "sh"])
def test_stage_out_reports_each_uploaded_file(tmp_path, stubbed_curl, shell, monkeypatch):
    data = tmp_path / "data"
    (data / "dir" / "sub").mkdir(parents=True)
    (data / "out.txt").write_text("hello")
    (data / "dir" / "a.txt").write_text("a")
    (data / "dir" / "sub" / "b c.txt").write_text("bbb")
    (data / "dir" / "new\nline.txt").write_text("n")  # uploaded, not reported
    (data / "w").mkdir()
    (data / "w" / "x.txt").write_text("xx")
    (data / "w" / "y.log").write_text("no match")
    confs = [
        {"container_path": str(data / "out.txt"), "url": HTTP, "type": "FILE"},
        {"container_path": str(data / "dir"), "url": HTTP, "type": "DIRECTORY"},
        {"container_path": str(data / "w" / "*.txt"), "url": HTTP, "type": "FILE", "path_prefix": f"{data}/"},
        {"container_path": str(data / "missing.txt"), "url": HTTP, "type": "FILE"},
    ]
    workdir = tmp_path / "working"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    result = subprocess.run([shell, "-c", f"{job_script('', 'true', stage_out_script(confs))}; return_code=$?; "
                                          f"echo $return_code > return_code;"], capture_output=True, text=True)
    assert Path("return_code").read_text() == "0\n", result.stderr
    assert result.stdout == ""
    logs = output_file_logs(Path(STAGE_OUT_REPORT_FILE).read_text(), confs)
    assert sorted((log["path"], log["size_bytes"]) for log in logs) == sorted([
        (f"{data}/out.txt", "5"), (f"{data}/dir/a.txt", "1"), (f"{data}/dir/sub/b c.txt", "3"),
        (f"{data}/w/x.txt", "2")])
    assert len(stubbed_curl.read_text().splitlines()) == 5  # new\nline.txt was uploaded too
    assert all("-o /dev/null" in call for call in stubbed_curl.read_text().splitlines())


# --- The task's logs --------------------------------------------------------------------------------------------------

def _directory_task(monkeypatch, tmp_path, files: int, executor_command):
    out = tmp_path / "out"
    out.mkdir()
    for i in range(files):
        (out / f"file-with-a-rather-long-name-{i:05d}.txt").write_text("x" * (i % 7))
    workdir = tmp_path / "working"
    workdir.mkdir()
    pulsar = ScriptPulsar(workdir)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "curl").write_text(CURL_STUB)
    (tmp_path / "bin" / "curl").chmod(0o755)
    confs = [{"container_path": str(out), "url": "s3://bucket/run/out", "type": "DIRECTORY", "volume_name": "v",
              "upload": "s3_post", "key_prefix": "run/out/", "form_fields": {"policy": "p"}}]
    task, _ = run_task(monkeypatch, None, [TesTaskExecutor(image="ubuntu", command=executor_command)],
                       output_confs=confs, pulsar=pulsar)
    return task, out


def test_every_uploaded_file_is_listed_past_64_kib_of_reports(monkeypatch, tmp_path):
    # Before, the reports were read from the job's stdout, of which only the last 64 KiB were kept: entries were lost.
    files = 1500
    task, out = _directory_task(monkeypatch, tmp_path, files, ["true"])
    assert task.state == TesTaskState.COMPLETE
    assert files * len(f"{OUTPUT_REPORT_PREFIX}\t0\t1\tfile-with-a-rather-long-name-00000.txt\n") > \
           EXECUTOR_LOG_TAIL_BYTES
    assert len(task.output_file_logs) == files
    assert {log["url"] for log in task.output_file_logs} == {
        f"s3://bucket/run/out/file-with-a-rather-long-name-{i:05d}.txt" for i in range(files)}  # never presigned
    assert task.output_file_logs[0]["size_bytes"] == str(len((out / Path(task.output_file_logs[0]["path"]).name)
                                                           .read_text()))


def test_an_executor_cannot_forge_entries(monkeypatch, tmp_path):
    # Its stdout goes to its own file (#67), and a report file it writes is replaced by the stage-out's.
    forged = f"{OUTPUT_REPORT_PREFIX}\\t0\\t99\\tforged.txt"
    task, _ = _directory_task(monkeypatch, tmp_path, 1, [
        "sh", "-c", f"printf '{forged}\\n'; printf '{forged}\\n' > {STAGE_OUT_REPORT_FILE}"])
    assert task.state == TesTaskState.COMPLETE
    assert [Path(log["path"]).name for log in task.output_file_logs] == ["file-with-a-rather-long-name-00000.txt"]
