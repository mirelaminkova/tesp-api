# Large-file transfers (issue #6): a file bigger than tesp-api's memory limit goes through an input and an output, over
# S3 (MinIO) and over HTTP (tests/upload_server.py). Inputs and outputs are moved by curl/wget in the stage containers
# that Pulsar runs, so tesp-api never holds file contents; these tests guard that. Run them with tesp-api's memory
# capped below the file size (tests/compose.mem-limit.yaml), as CI does:
#
#   docker compose -f docker-compose.yaml -f tests/compose.mem-limit.yaml --profile pulsar up -d --build
#   python3 tests/upload_server.py &
#   python3 -m pytest tests/large_file_tests.py -v
#
# LARGE_FILE_MIB sets the file size (default 1024). The file is generated, never committed.
import hashlib
import os
import subprocess
import time

import pytest

from tests.smoke_tests import (S3_TEST_BUCKET, _assert_no_s3_secrets_stored, _gnv, _post_request,
                               _s3_ready_test_client, _wait_for_state, script_directory)

FILE_SIZE = int(os.environ.get("LARGE_FILE_MIB", "1024")) * 1024 * 1024
CHUNK = 8 * 1024 * 1024
TIMEOUT = 900  # seconds per task; _wait_for_state polls once a second
UPLOADED = os.path.join(script_directory, "uploaded_data")  # served and written by tests/upload_server.py
UPLOAD_URL = "http://172.17.0.1:5000/upload"


def _docker_inspect(field):
    return subprocess.check_output(["docker", "inspect", "-f", f"{{{{{field}}}}}", "tesp-api"], text=True).strip()


def _sha256_chunks(chunks):
    digest, size = hashlib.sha256(), 0
    for chunk in chunks:
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _sha256_file(path):
    with open(path, "rb") as f:
        return _sha256_chunks(iter(lambda: f.read(CHUNK), b""))


@pytest.fixture(scope="module", autouse=True)
def tesp_api_memory_capped():
    """The tests prove nothing unless tesp-api would run out of memory buffering the file; afterwards it must not have."""
    limit = int(_docker_inspect(".HostConfig.Memory"))
    assert 0 < limit < FILE_SIZE, f"tesp-api memory limit {limit} must be set and below the file size {FILE_SIZE}"
    restarts = _docker_inspect(".RestartCount")
    yield
    assert _docker_inspect(".State.OOMKilled") == "false", "tesp-api ran out of memory"
    assert _docker_inspect(".State.Running") == "true"
    assert _docker_inspect(".RestartCount") == restarts
    try:  # cgroup v2: highest memory use since the container started, for the job log
        peak = subprocess.check_output(["docker", "exec", "tesp-api", "cat", "/sys/fs/cgroup/memory.peak"], text=True)
        print(f"tesp-api memory.peak: {int(peak) // 2**20} MiB (limit {limit // 2**20} MiB)")
    except (subprocess.CalledProcessError, ValueError):
        pass


@pytest.fixture(scope="module")
def large_file():
    """A FILE_SIZE file of random bytes below the upload server's root, and its SHA-256."""
    path = os.path.join(UPLOADED, "large_input.bin")
    os.makedirs(UPLOADED, exist_ok=True)
    digest = hashlib.sha256()
    with open(path, "wb") as f:
        for _ in range(FILE_SIZE // CHUNK):
            chunk = os.urandom(CHUNK)
            digest.update(chunk)
            f.write(chunk)
    yield path, digest.hexdigest()
    os.remove(path)


def _copy_and_check(in_path, out_path, sha256):
    """Executor that checks the staged input and copies it to the output path."""
    script = f"echo '{sha256}  {in_path}' | sha256sum -c - && mkdir -p {os.path.dirname(out_path)} && cp {in_path} {out_path}"
    return [{"image": "ubuntu", "command": ["sh", "-c", script]}]


def _run(task):
    task_id = _gnv(_post_request("/v1/tasks", payload=task).json(), "id")
    started = time.monotonic()
    assert _wait_for_state(task_id, "COMPLETE", TIMEOUT)
    print(f"task {task_id}: {FILE_SIZE // 2**20} MiB in and out in {time.monotonic() - started:.0f} s")
    return task_id


# S3 input (presigned GET) and S3 FILE output (presigned PUT, single request: files up to 5 GiB, see #34).
def test_large_s3_input_and_output(large_file):
    path, sha256 = large_file
    client = _s3_ready_test_client()
    prefix = f"large/{time.time_ns()}"
    in_key, out_key = f"{prefix}/input.bin", f"{prefix}/output.bin"
    with open(path, "rb") as f:
        client.put_object(Bucket=S3_TEST_BUCKET, Key=in_key, Body=f)
    try:
        task_id = _run({
            "inputs": [{"path": "/data/large_in/input.bin", "url": f"s3://{S3_TEST_BUCKET}/{in_key}", "type": "FILE"}],
            "outputs": [{"path": "/data/large_out/output.bin", "url": f"s3://{S3_TEST_BUCKET}/{out_key}",
                         "type": "FILE"}],
            "executors": _copy_and_check("/data/large_in/input.bin", "/data/large_out/output.bin", sha256),
        })
        body = client.get_object(Bucket=S3_TEST_BUCKET, Key=out_key)["Body"]
        assert _sha256_chunks(iter(lambda: body.read(CHUNK), b"")) == (sha256, FILE_SIZE)
        _assert_no_s3_secrets_stored(task_id)
    finally:
        for key in (in_key, out_key):
            client.delete_object(Bucket=S3_TEST_BUCKET, Key=key)


# HTTP input (curl GET from the upload server) and HTTP FILE output (multipart POST to it).
def test_large_http_input_and_output(large_file):
    path, sha256 = large_file
    uploaded = os.path.join(UPLOADED, "large_http_output.bin")
    if os.path.exists(uploaded):
        os.remove(uploaded)
    try:
        _run({
            "inputs": [{"path": "/data/large_in/input.bin", "type": "FILE",
                        "url": "http://172.17.0.1:5000/uploaded_data/large_input.bin"}],
            "outputs": [{"path": "/data/large_out/large_http_output.bin", "url": UPLOAD_URL, "type": "FILE"}],
            "executors": _copy_and_check("/data/large_in/input.bin", "/data/large_out/large_http_output.bin", sha256),
        })
        assert _sha256_file(uploaded) == (sha256, FILE_SIZE)
    finally:
        if os.path.exists(uploaded):
            os.remove(uploaded)
