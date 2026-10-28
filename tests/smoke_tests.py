
import time
import requests
import json
import os

import pytest
import shutil
import subprocess

import sys
sys.path.append('/app')

from tesp_api.utils.commons import Commons

base_url = "http://localhost:8080"

script_directory = os.path.dirname(os.path.abspath(__file__))


def _gnv(data, key):
    """Get a nested value from a dictionary using dot notation."""
    keys = key.split(".")
    current_data = data
    for k in keys:
        try:
            current_data = current_data[k]
        except KeyError:
            return False
    return current_data

def _get_request(url):
    """Sent a GET HTTP request, test the status, jsonization and return the response in JSON."""
    resp = requests.get(f"{base_url}{url}")
    assert resp.status_code == 200
    assert resp.json()
    return resp.json()

def _post_request(url, payload={}):
    """Sent a POST HTTP request, test the status, jsonization and return the response in JSON."""
    resp = requests.post(f"{base_url}{url}", json=payload)
    assert resp.status_code == 200
    assert resp
    return resp

def _open_json(filename):
    with open(f"{script_directory}/test_jsons/{filename}", 'r') as f:
        data = f.read()
    return json.loads(data)

def _wait_for_state(id, expected_state, timeout = 10):
    for i in range(timeout):
        resp = _get_request(f"/v1/tasks/{id}")
        actual_state = _gnv(resp, "state")
        
        # end on right state
        if actual_state == expected_state:
            return resp
        
        # raise exception on wrong state (not right state and not RUNNING state)
        if actual_state != "INITIALIZING" and actual_state != "QUEUED" and actual_state != "RUNNING":
            raise Exception(f"_wait_for_state(): unexpected state. ID: {id}, expected: {expected_state}, given: {actual_state}")
        
        time.sleep(1)
        
    raise Exception(f"_wait_for_state(): timeout. ID: {id}, state: {actual_state}")

def _test_simple(json, timeout, expected_state = 'COMPLETE'):
    # submit task
    json = _open_json(json)
    resp = _post_request("/v1/tasks", payload=json).json()

    # get ID of the already started task
    id = _gnv(resp, "id")

    # try to get status of the task
    return _wait_for_state(id, expected_state, timeout)

def _test_activity(json, timeout_running, timeout_complete, expected_state = 'COMPLETE'):
    # submit task
    json = _open_json(json)
    resp = _post_request("/v1/tasks", payload=json).json()

    # get ID of the already started task
    id = _gnv(resp, "id")

    # try to get status of the task
    if not _wait_for_state(id, 'RUNNING', timeout_running):
        return False
    return _wait_for_state(id, expected_state, timeout_complete)

    task_response = _wait_for_state(id, expected_state, timeout_complete)

    # Retrieve and print resources if they exist
    resources = _gnv(task_response, "resources")
    if resources:
        print(f"Resources for task {id}: {resources}")
    else:
        print(f"No resources found for task {id}.")

    return task_response

def _test_sequence_simple(jsons, timeout, expected_state = 'COMPLETE'):
    for file_name in jsons:
        json = f'{file_name}.json'
        if not _test_simple(json, timeout, expecetd_state):
            return False

    return True

def _test_sequence_activity(jsons, timeout_running, timeout_complete, expected_state = 'COMPLETE'):
    for file_name in jsons:
        json = f'{file_name}.json'
        if not _test_activity(json, timeout_running, timeout_complete, expected_state):
            return False

    return True

def test_service_info():
    data = _get_request("/v1/service-info")    

    # see https://github.com/ga4gh/task-execution-schemas/releases
    TES_API_VERSION = '1.0.0'
    
    assert _gnv(data, "id")
    assert _gnv(data, "name")
    assert _gnv(data, "type.group") == "org.ga4gh"
    assert _gnv(data, "type.artifact") == "tes"
    assert _gnv(data, "type.version") == TES_API_VERSION
    assert _gnv(data, "organization.name")
    assert _gnv(data, "organization.url")
    assert _gnv(data, "version") == Commons.get_service_version()

# Submits task with expected state: COMPLETE.
def test_submit_task_complete():
    assert _test_activity("state_true.json", 10, 60, 'COMPLETE')

# Submits task with expected state: EXECUTOR_ERROR.
def test_submit_task_fail():
    assert _test_activity("state_false.json", 10, 60, 'EXECUTOR_ERROR')

# Submits multiple tasks with expected state: COMPLETE.
def test_submit_task_multi_complete():
    assert _test_activity("multi_true.json", 10, 60, 'COMPLETE')

# Submits multiple tasks with expected state: EXECUTOR_ERROR.
def test_submit_task_multi_fail():
    jsons = ["multi_false_1", "multi_false_2", "multi_false_3"]
    assert _test_sequence_activity(jsons, 10, 60, 'EXECUTOR_ERROR')

# Lists tasks.
def test_get_task_list():
    data = _get_request("/v1/tasks")

    tasks = _gnv(data, "tasks")
    assert tasks
    assert isinstance(tasks, list)

# HTTP download (content checked) and direct input.
def test_inputs():
    assert _test_simple("inputs.json", 120)

# Test directory as an input.
def test_dir_input():
    assert _test_simple("dir-io.json", 120)

# HTTP inputs behind redirects (issue #41): stage-in follows them and stores the target, not the redirect body.
def test_http_input_redirect():
    task = {
        "inputs": [{"path": "/data/redirected",
                    "url": "http://172.17.0.1:5000/redirect/redirect/test_data/test.txt", "type": "FILE"}],
        "executors": [{"image": "ubuntu", "command": ["grep", "-qx", "this is a test file", "/data/redirected"]}]
    }
    task_id = _gnv(_post_request("/v1/tasks", payload=task).json(), "id")
    assert _wait_for_state(task_id, "COMPLETE", 120)

# FILE and DIRECTORY outputs uploaded over HTTP (multipart POST to tests/upload_server.py), plus missing outputs,
# which are skipped: TES has no "optional" flag for outputs, and clients such as Nextflow check for missing files
# themselves. Directory outputs keep their structure below the upload root.
def test_outputs():
    token = f"http-{time.time_ns()}"
    upload_url = "http://172.17.0.1:5000/upload"
    script = (
        "mkdir -p /data/http_out/results/outputs_http/sub"
        f" && echo file-{token} > /data/http_out/outputs_http_file.txt"
        f" && echo top-{token} > /data/http_out/results/outputs_http/top.txt"
        f" && echo nested-{token} > /data/http_out/results/outputs_http/sub/nested.txt"
    )
    task = {
        "outputs": [
            {"path": "/data/http_out/outputs_http_file.txt", "url": upload_url, "type": "FILE"},
            {"path": "/data/http_out/results", "url": upload_url, "type": "DIRECTORY"},
            {"path": "/data/http_out/outputs_http_missing.txt", "url": upload_url, "type": "FILE"},
            {"path": "/data/http_out/outputs_http_missing_dir", "url": upload_url, "type": "DIRECTORY"},
        ],
        "executors": [{"image": "ubuntu", "command": ["sh", "-c", script]}]
    }
    task_id = _gnv(_post_request("/v1/tasks", payload=task).json(), "id")
    assert _wait_for_state(task_id, "COMPLETE", 120)

    uploaded = f"{script_directory}/uploaded_data"
    expected = {
        "outputs_http_file.txt": f"file-{token}\n",
        "outputs_http/top.txt": f"top-{token}\n",
        "outputs_http/sub/nested.txt": f"nested-{token}\n",
    }
    for name, content in expected.items():
        with open(f"{uploaded}/{name}") as f:
            assert f.read() == content, name
    assert not os.path.exists(f"{uploaded}/outputs_http_missing.txt")
    _assert_skipped(task_id, ["/data/http_out/outputs_http_missing.txt", "/data/http_out/outputs_http_missing_dir"])

def _assert_skipped(task_id, paths):
    """The stage-out script reports each skipped output on a line of its own in the task's stderr."""
    task = _get_request(f"/v1/tasks/{task_id}?view=FULL")
    stderr_lines = [line for log in task["logs"] for executor_log in log["logs"]
                    for line in (executor_log.get("stderr") or "").splitlines()]
    for path in paths:
        assert f"tesp-api: output {path} not found, skipped" in stderr_lines, path

# Downloads and copies file to the shared volume and displays its content with two separate executors.
def test_volumes():
    assert _test_simple("volumes.json", 100)

# Verifies that environment variables from envs.json are correctly echoed to output files with expected content. 
def test_envs():
    assert _test_simple("envs.json", 100)
    
    expected_files = {
        f"{script_directory}/uploaded_data/env_test_1": "first upload successful\n",
        f"{script_directory}/uploaded_data/env_test_2": "second upload successful\n"
    }
    
    # Verify that each file was created with the expected content
    for path, expected_content in expected_files.items():
        # Open the file and read its content
        with open(path, 'r') as f:
            content = f.read()
        assert content == expected_content, f"File {path} does not contain the expected content."

# Displays CPU information by reading /proc/cpuinfo in different ways, prints the working directory by running pwd from /bin.
def test_workdir():
    assert _test_simple("workdir.json", 120)

def _executor_logs(task_id, stream):
    """Return all ExecutorLog.<stream> values of a task, fetched with view=FULL."""
    task = _get_request(f"/v1/tasks/{task_id}?view=FULL")
    return "".join(
        executor_log.get(stream) or ""
        for task_log in task.get("logs") or []
        for executor_log in task_log.get("logs") or []
    )

def _remove_uploaded(filename):
    """Remove a stale upload from an earlier run so it cannot mask a failed upload."""
    path = f"{script_directory}/uploaded_data/{filename}"
    if os.path.exists(path):
        os.remove(path)

def _read_uploaded(filename):
    with open(f"{script_directory}/uploaded_data/{filename}", 'r') as f:
        return f.read()

# Executor.stdin is piped to the command; its output shows up in ExecutorLog.stdout (view=FULL).
def test_stdin():
    task = _test_simple("stdin.json", 120)
    assert "HELLO-WORLD-STDIN" in _executor_logs(_gnv(task, "id"), "stdout")

# Executor.stdout is written to the given container path (checked by a second executor and by
# uploading it as an output) and is also reported in ExecutorLog.stdout (view=FULL).
def test_stdout():
    _remove_uploaded("stdout-test")
    task = _test_simple("stdout.json", 120)
    assert _read_uploaded("stdout-test") == "hello-world-stdout\n"
    assert "hello-world-stdout" in _executor_logs(_gnv(task, "id"), "stdout")

# Executor.stderr is written to the given container path and is also reported in
# ExecutorLog.stderr (view=FULL).
def test_stderr():
    _remove_uploaded("stderr-test")
    task = _test_simple("stderr.json", 120)
    assert _read_uploaded("stderr-test") == "hello-world-stderr\n"
    assert "hello-world-stderr" in _executor_logs(_gnv(task, "id"), "stderr")

# Executor env values reach the container literally: the host shell does not expand them, and
# `singularity --env` would split values containing ',' or '=' and trim quotes.
def test_env_values_are_not_expanded():
    env = {"INJECT": "$(echo pwned)", "TICKS": "`echo pwned` $HOME", "CSV": 'a=1,b="2"'}
    task = {
        "executors": [{
            "image": "ubuntu",
            "command": ["sh", "-c", 'printf "[%s]\\n" "$INJECT" "$TICKS" "$CSV"'],
            "env": env
        }]
    }
    task_id = _gnv(_post_request("/v1/tasks", payload=task).json(), "id")
    assert _wait_for_state(task_id, "COMPLETE", 120)
    stdout = _executor_logs(task_id, "stdout")
    for value in env.values():
        assert f"[{value}]" in stdout

# Invalid executor env names are refused at task creation, naming the field.
def test_invalid_env_name_rejected():
    task = {"executors": [{"image": "ubuntu", "command": ["true"], "env": {"BAD;NAME": "x"}}]}
    resp = requests.post(f"{base_url}/v1/tasks", json=task)  # not _post_request, which expects 200
    assert resp.status_code == 400
    assert "executors[0].env" in resp.text

# Images that are host paths (run by Singularity as the container root) are refused at task creation, naming the
# field without echoing the value.
@pytest.mark.parametrize("image", ["/", "/images/x.sif", "./rel", "instance://x"])
def test_host_path_image_rejected(image):
    task = {"executors": [{"image": image, "command": ["true"]}]}
    resp = requests.post(f"{base_url}/v1/tasks", json=task)  # not _post_request, which expects 200
    assert resp.status_code == 400
    assert "executors[0].image" in resp.text
    if image != "/":
        assert image not in resp.text

# Cancels a concrete task in RUNNING state.
def test_task_cancel():
    response = _test_simple("cancel.json", 60, "RUNNING")
    task_id = _gnv(response, "id")
    assert task_id
    _post_request(f"/v1/tasks/{task_id}:cancel")
    assert _wait_for_state(task_id, "CANCELED", 60)
    # Polling notices the cancellation and cancels the Pulsar job too (issue #49); the old loop never did.
    assert _wait_for_pulsar_cancel(task_id, 60)

def _wait_for_pulsar_cancel(task_id, timeout):
    """Whether tesp-api asked Pulsar to cancel the task's job.

    Reads the DEBUG log line of each Pulsar request (log_pulsar_request), so it needs the compose container name
    tesp-api, logging.level = "DEBUG" and Pulsar's REST interface (over AMQP, cancel is a queue message).
    """
    for _ in range(timeout):
        logs = subprocess.run(["docker", "logs", "tesp-api"], capture_output=True, text=True)
        if f"/jobs/{task_id}/cancel>" in logs.stdout + logs.stderr:
            return True
        time.sleep(1)
    return False

# Runs longer than the legacy poll limit of the compose test profile (pulsar.status.max_polls = 5 at a 1 s interval),
# which used to fail the task while its job kept running (issue #49).
def test_task_outlives_legacy_poll_limit():
    response = _test_simple("long_running.json", 120)
    assert "finished" in _executor_logs(_gnv(response, "id"), "stdout")

# Checks whether given task exceeds available resources.
def test_resource_check_with_limits():
    json_data = _open_json('resource_check.json')

    cpu_cores = _gnv(json_data, "resources.cpu_cores")
    ram_gb = _gnv(json_data, "resources.ram_gb")

    max_cpu_cores = 4
    max_ram_gb = 3.8

    assert cpu_cores <= max_cpu_cores, f"CPU cores exceed the limit: {cpu_cores} > {max_cpu_cores}"
    assert ram_gb <= max_ram_gb, f"RAM exceeds the limit: {ram_gb} > {max_ram_gb}"

    print(f"Requested resources: CPU cores = {cpu_cores}, RAM = {ram_gb} GB (Limits: {max_cpu_cores} cores, {max_ram_gb} GB RAM)")

# A task with resources.cpu_cores and ram_gb (sent by every Nextflow task) completes with the default
# container.resource_limits, also under singularity where apptainer cannot apply the limits (issue #38).
def test_task_with_resources_completes():
    assert _test_simple("resources.json", 120)

# Remove uploaded_data directory
@pytest.fixture(scope="session", autouse=True)
def cleanup_uploaded_data():
    yield  # Wait until all tests are done

    uploaded_path = os.path.join(os.path.dirname(__file__), "uploaded_data")
    if os.path.isdir(uploaded_path):
        try:
            shutil.rmtree(uploaded_path)
            print(f"Removed uploaded data: {uploaded_path}")
        except Exception as e:
            print(f"Failed to remove {uploaded_path}: {e}")

    # container clean up
    try:
        output = subprocess.check_output([
            "docker", "ps", "-q", "--filter", "ancestor=ubuntu", "--filter", "status=running"
        ]).decode().strip().splitlines()

        for container_id in output:
            subprocess.run(["docker", "rm", "-f", container_id])
            print(f"Removed container: {container_id}")
    except Exception as e:
        print(f"Failed to remove sleep containers: {e}")


# S3 inputs (issue #3). MinIO runs in the compose "pulsar" profile; tesp-api gets the endpoint and
# credentials through TESP_API_S3__* in docker-compose.yaml. The URLs in the task carry no credentials.
S3_TEST_ENDPOINT = "http://172.17.0.1:9000"  # MinIO listens on the docker bridge only
S3_TEST_ACCESS_KEY = "tesp-test"
S3_TEST_SECRET_KEY = "tesp-test-secret"  # local MinIO test credentials, same as in docker-compose.yaml
S3_TEST_BUCKET = "tesp-test"

def _s3_test_client():
    import botocore.session
    from botocore.config import Config
    return botocore.session.get_session().create_client(
        "s3", endpoint_url=S3_TEST_ENDPOINT, region_name="us-east-1",
        aws_access_key_id=S3_TEST_ACCESS_KEY, aws_secret_access_key=S3_TEST_SECRET_KEY,
        config=Config(s3={"addressing_style": "path"}))

def _s3_ready_test_client():
    client = _s3_test_client()
    for _ in range(60):  # MinIO may still be starting; compose creates the bucket
        try:
            client.head_bucket(Bucket=S3_TEST_BUCKET)
            return client
        except Exception:
            time.sleep(2)
    raise RuntimeError(f"MinIO bucket {S3_TEST_BUCKET} not reachable at {S3_TEST_ENDPOINT}")

def _s3_put_test_object(key, body):
    _s3_ready_test_client().put_object(Bucket=S3_TEST_BUCKET, Key=key, Body=body)

def _assert_no_s3_secrets_stored(task_id):
    stored = json.dumps(_get_request(f"/v1/tasks/{task_id}?view=FULL"))
    for leaked in (S3_TEST_SECRET_KEY, "X-Amz-Signature", "X-Amz-Credential", "x-amz-signature",
                   "x-amz-credential"):
        # A plain `assert leaked not in stored` would print the stored task, secrets included, into the public CI log.
        assert leaked not in stored, f"{leaked!r} stored with task {task_id}"

# Downloads a plain s3://bucket/key input using the server-side S3 settings.
def test_s3_input():
    content = f"hello from s3 {time.time_ns()}"
    _s3_put_test_object("smoke/input.txt", content.encode())
    task = {
        "inputs": [{"path": "/data/s3_input", "url": f"s3://{S3_TEST_BUCKET}/smoke/input.txt", "type": "FILE"}],
        # The object has no trailing newline; grep -x still matches its single line.
        "executors": [{"image": "ubuntu", "command": ["grep", "-qx", content, "/data/s3_input"]}]
    }
    task_id = _gnv(_post_request("/v1/tasks", payload=task).json(), "id")
    assert _wait_for_state(task_id, "COMPLETE", 120)

    _assert_no_s3_secrets_stored(task_id)

# Legacy s3://user:pass@host:port/bucket/key URLs are refused and not stored.
def test_s3_legacy_url_rejected():
    task = {
        "inputs": [{"path": "/data/in", "url": "s3://user:legacy-password@172.17.0.1:9000/tesp-test/in.txt"}],
        "executors": [{"image": "ubuntu", "command": ["true"]}]
    }
    resp = requests.post(f"{base_url}/v1/tasks", json=task)  # not _post_request, which expects 200
    assert resp.status_code == 400
    assert "inputs[0].url" in resp.text
    assert "legacy-password" not in resp.text

# FILE (presigned PUT) and DIRECTORY (presigned POST policy limited to the key prefix) outputs to plain s3://bucket/key
# URLs, plus missing outputs, which are skipped. Directory outputs keep their structure below the URL prefix.
def test_s3_outputs():
    client = _s3_ready_test_client()
    token = f"s3-{time.time_ns()}"
    prefix = f"smoke/outputs/{token}"
    script = (
        "mkdir -p '/data/s3_out/results/sub dir'"
        f" && echo file-{token} > /data/s3_out/result.txt"
        f" && echo top-{token} > /data/s3_out/results/top.txt"
        f" && echo nested-{token} > '/data/s3_out/results/sub dir/nested.txt'"
    )
    task = {
        "outputs": [
            {"path": "/data/s3_out/result.txt", "url": f"s3://{S3_TEST_BUCKET}/{prefix}/result.txt", "type": "FILE"},
            {"path": "/data/s3_out/results", "url": f"s3://{S3_TEST_BUCKET}/{prefix}/results", "type": "DIRECTORY"},
            {"path": "/data/s3_out/missing.txt", "url": f"s3://{S3_TEST_BUCKET}/{prefix}/missing.txt", "type": "FILE"},
            {"path": "/data/s3_out/missing_dir", "url": f"s3://{S3_TEST_BUCKET}/{prefix}/missing_dir",
             "type": "DIRECTORY"},
        ],
        "executors": [{"image": "ubuntu", "command": ["sh", "-c", script]}]
    }
    task_id = _gnv(_post_request("/v1/tasks", payload=task).json(), "id")
    assert _wait_for_state(task_id, "COMPLETE", 120)

    listed = client.list_objects_v2(Bucket=S3_TEST_BUCKET, Prefix=f"{prefix}/")
    objects = {
        obj["Key"]: client.get_object(Bucket=S3_TEST_BUCKET, Key=obj["Key"])["Body"].read().decode()
        for obj in listed.get("Contents", [])
    }
    assert objects == {
        f"{prefix}/result.txt": f"file-{token}\n",
        f"{prefix}/results/top.txt": f"top-{token}\n",
        f"{prefix}/results/sub dir/nested.txt": f"nested-{token}\n",
    }
    _assert_skipped(task_id, ["/data/s3_out/missing.txt", "/data/s3_out/missing_dir"])
    _assert_no_s3_secrets_stored(task_id)

# The job script is not traced (issue #18): executor env values, presigned S3 URLs and the host container commands
# must not show up in the task's stored stdout/stderr logs.
def test_job_script_not_in_task_logs():
    _s3_put_test_object("smoke/trace/input.txt", b"in")  # staged in only so the job holds a presigned GET URL
    token = f"trace-{time.time_ns()}"
    task = {
        "inputs": [{"path": "/data/trace/in.txt", "url": f"s3://{S3_TEST_BUCKET}/smoke/trace/input.txt", "type": "FILE"}],
        "outputs": [{"path": "/data/trace/out.txt", "url": f"s3://{S3_TEST_BUCKET}/smoke/trace/{token}.txt",
                     "type": "FILE"}],
        "executors": [{"image": "ubuntu", "command": ["sh", "-c", "echo hi && echo hi > /data/trace/out.txt"],
                       "env": {"SECRET": "s3cr3t"}}]
    }
    task_id = _gnv(_post_request("/v1/tasks", payload=task).json(), "id")
    assert _wait_for_state(task_id, "COMPLETE", 120)

    stdout, stderr = _executor_logs(task_id, "stdout"), _executor_logs(task_id, "stderr")
    assert "hi" in stdout
    for leaked in ("s3cr3t", "X-Amz-Signature", "singularity exec", "docker run"):
        assert leaked not in stdout, leaked
        assert leaked not in stderr, leaked
    _assert_no_s3_secrets_stored(task_id)
