# Testing

Tests for TESP-API come in two kinds:

- **Unit tests** (`tests/unit/`) need no running services:

  ```bash
  python3 -m pytest tests/unit
  ```

- **Integration tests** (described below) run against a live TESP-API instance at `http://localhost:8080`.

CI (`.github/workflows/ci.yaml`) runs both suites on every pull request and on pushes to `main`.

## Prerequisites

Start TESP-API + MongoDB + Pulsar from the project root:

```bash
docker compose --profile pulsar up -d --build
```

## Running Tests

### 1. Start the upload server

For I/O tests (`test_inputs`, `test_dir_input`, `test_volumes`, `test_envs`), the test fixtures download/upload files via HTTP. Start the local file server:

```bash
python3 tests/upload_server.py
```

This serves `tests/test_data/` on port 5000 and accepts uploads to `tests/uploaded_data/`.

### 2. Run smoke tests

```bash
python3 -m pytest tests/smoke_tests.py -v
```

### Load/stress test (100 concurrent tasks)

```bash
python3 -m pytest tests/load_stress_test.py -v
```

### GA4GH TES conformance suite

With the stack up (see Prerequisites), run [ga4gh/openapi-test-runner](https://github.com/ga4gh/openapi-test-runner) against it:

```bash
bash tests/conformance.sh
```

The script fetches the runner at a pinned commit into `.conformance/`, installs it in a virtualenv there, and runs it once per TES version (`1.0.0` and `1.1.0` by default). It writes `report.json`, `run.log` and a `summary.md` table to `.conformance/report/<version>/` for each version. It exits 0 when every test passes, 1 when any test fails and 2 when the run itself breaks. The only change it makes to the suite is lowering the per-task polling timeout from 3600 s to `POLL_TIMEOUT` (600 s by default). The header of `tests/conformance.sh` lists the other variables it reads (`TES_URL`, `TES_VERSIONS`, `WORK_DIR`).

CI runs the same script in the `conformance` job and uploads `.conformance/report/` as the `conformance-report` artefact. Failures don't fail CI yet; each one is tracked as a `tes-compliance` sub-issue of #2.

## Platform Notes

The test fixtures use `http://172.17.0.1:5000` to reach the host from inside containers. This works on **Linux** (Docker bridge gateway IP).


## Test Fixtures

All task JSON fixtures are in `test_jsons/`. Key files:

| Fixture | Description | Requires upload_server |
|---|---|---|
| `state_true.json` | Simple task that should succeed | No |
| `state_false.json` | Task designed to fail | No |
| `cancel.json` | Long-running task (`sleep infinity`) for cancellation | No |
| `multi_true.json` | Three sequential executors | No |
| `inputs.json` | HTTP download + inline content | **Yes** |
| `dir-io.json` | Directory input/output via HTTP | **Yes** |
| `volumes.json` | Shared volume between executors | **Yes** |
| `envs.json` | Environment variables with output upload | **Yes** |
| `workdir.json` | Working directory test | No |

## DTS (Data Transfer Services)

The `docker/dts/` directory contains an **alternative** test infrastructure with MinIO (S3), FTP, and HTTP services running in containers. At the moment, only HTTP service is compatible with the smoke tests.

## S3

`test_s3_input` and `test_s3_legacy_url_rejected` use the `minio` service from the root `docker-compose.yaml` (profile `pulsar`, port 9000), which creates the `tesp-test` bucket; the test uploads its object from the host. MinIO is published on the docker bridge address only (`172.17.0.1:9000`). `tesp-api` only signs URLs for `http://172.17.0.1:9000` with the test credentials from `docker-compose.yaml`; the Pulsar staging containers download from there. Like the other fixtures this relies on the default Docker bridge gateway on Linux.

### Using DTS instead of upload_server.py

```bash
# Start DTS (from project root)
cd docker/dts && docker compose up -d --build && cd ../..

# Run tests (DTS serves on the same port 5000)
python3 -m pytest tests/smoke_tests.py -v
```

DTS mounts `tests/test_data/` and `tests/uploaded_data/` so the same fixture URLs work.

## Cleanup

The test suite automatically cleans up:
- `uploaded_data/` directory created by upload tests
- Leftover Ubuntu containers from failed tests
