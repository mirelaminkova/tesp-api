# TESP API

[![GitHub issues](https://img.shields.io/github/issues/CESNET/tesp-api)](https://github.com/CESNET/tesp-api/issues)
[![poetry](https://img.shields.io/badge/maintained%20with-poetry-informational.svg)](https://python-poetry.org/)
[![python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/download)
[![last-commit](https://img.shields.io/github/last-commit/CESNET/tesp-api)]()

This is a task execution microservice based on the [TES standard](https://github.com/ga4gh/task-execution-schemas) that sends job executions to [Pulsar](https://github.com/galaxyproject/pulsar) application. 

Read about our project on the [Galaxy Hub](https://galaxyproject.org/news/2025-10-06-tesp-api/) and [e-INFRA CZ Blog](https://blog.e-infra.cz/blog/tesp-api/).

This effort is part of the [EuroScienceGateway](https://galaxyproject.org/projects/esg/) project.
For more details on TES, see the [Task Execution Schemas documentation](https://ga4gh.github.io/task-execution-schemas/docs/). 
Pulsar is a Python server application that allows a [Galaxy](https://github.com/galaxyproject/galaxy) server to run jobs on remote systems. 

## Quick start

### Deploy
The most straightforward way to deploy the TESP is to use Docker Compose.

#### API and DB services (default):
```
docker compose up -d
```
Starts the API and MongoDB containers. Configure an external Pulsar in `settings.toml`
(default points to `http://localhost:8913`). REST is the default; AMQP is used only
if `pulsar.amqp_url` is set.

#### With pulsar_rest service:
```
docker compose --profile pulsar up -d
```
Starts a local Pulsar REST container in the same compose network.

<br />

Depending on your Docker and Docker Compose installation, you may need to use `docker-compose` (with hyphen) instead.

You might encounter a timeout error in container runtime which can be solved by correct `mtu` configuration either in the `docker-compose.yaml`:
```
networks:
  default:
    driver: bridge
    driver_opts:
      com.docker.network.driver.mtu: 1442
```
 or directly in your `/etc/docker/daemon.json`:
```
{
  "mtu": 1442
}
```

The Data Transfer Services (HTTP/S3/FTP) are defined in [docker/dts](docker/dts/README.md)
and run via a separate compose file.

&nbsp;
### Usage
If the TESP is running, you can try to submit a task. One way is to use cURL. Although the project is still in development, the TESP should be compatible with TES so you can try TES clients such as Snakemake or Nextflow. The example below shows how to submit task using cURL.

#### 1. Create JSON file
The first step you need to take is to prepare JSON file with the task. For inspiration you can use [tests/test_jsons](tests/test_jsons) located in this repository, or [TES documentation](https://ga4gh.github.io/task-execution-schemas/docs/).  

Example JSON file:
```
{
  "inputs": [
    {
      "url": "http://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.5.5.tar.xz",
      "path": "/data/kernel.tar.gz",
      "type": "FILE"
    }
  ],
  "executors": [
    {
      "image": "ubuntu:20.04",
      "command": [
        "/bin/sha1sum",
        "./kernel.tar.gz"
      ],
      "workdir": "/data/",
      "stdout": "/tmp/stdout.log",
      "stderr": "/tmp/stderr.log"
    }
  ]
}
```
#### 2. Submit task
Please check the URL of the running TES and the file with the task you just created.
```
curl http://localhost:8080/v1/tasks -X POST -H "Content-Type: application/json" -d $(sed -e "s/ //g" example.json | tr -d '\n')
```
(The only reason for the subshell is to remove whitespaces and newlines.)  
After the task is submitted, the endpoint returns the task ID. This is useful to check the task status.

#### 3. Check the task status
There are more useful endpoints to check the task status.  

List all tasks:
```
curl "http://localhost:8080/v1/tasks"
```

Check the specific task status (enter your task ID):
```
curl "http://localhost:8080/v1/tasks/<id>?view=FULL"
```


&nbsp;
## Getting Started
Repository contains `docker-compose.yaml` file with infrastructure setup for current functionality which can be used to
immediately start the project in **DEVELOPMENT** environment. This is convenient for users and contributors as there is no need to manually install and
configure all the services which `TESP API` requires for it to be fully functional. While this is the easiest approach to start
the project, for "first timers" it's recommended to follow this readme to understand all the services and tools used across the project.  
**Also have a detailed look at [Current Docker services](#current-docker-services) section of this readme before starting up the infrastructure for the first time**.
  
_**!! DISCLAIMER**_:  
Project is currently in the development phase only and should not be used in production environments yet. If you really
wish to set up a production environment despite missing features, tests etc ... then following contents will show what needs to be done.  

### Requirements
_You can work purely with [docker](https://www.docker.com/) and [docker-compose](https://docs.docker.com/compose/) only
instead of starting the project locally without `docker`. In that case only those two dependencies are relevant for you._

| dependency     | version  | note                                                                                                                                                          |
|----------------|:--------:|---------------------------------------------------------------------------------------------------------------------------------------------------------------|
| docker         | 20.10.0+ | _**latest** is preferred_                                                                                                                                     |
| docker-compose | 1.28.0+  | -                                                                                                                                                             |  
| python         | 3.10.0+  | -                                                                                                                                                             |
| pip            | python3  | _**21.3.1** in case of problems_                                                                                                                              |
| poetry         | 1.1.13+  | _pip install poetry_                                                                                                                                          |
| mongodb        |   4.4+   | _docker-compose uses latest_                                                                                                                                  |
| pulsar         | 0.14.13  | _actively trying to support latest. Must have access to docker with the same host as pulsar application itself_                                               |
| ftp server     |    -     | _optional for I/O testing. The [docker/dts](docker/dts/README.md) stack provides FTP/S3/HTTP services_. |  

&nbsp;
### Configuring TESP API
`TESP API` uses [dynaconf](https://www.dynaconf.com/) for its configuration. Configuration is currently set up by using
[./settings.toml](https://github.com/CESNET/tesp-api/blob/main/settings.toml) file. This file declares sections which represent different environments for `TESP API`. Default section
is currently used for local development without `docker`. Also, all the properties from default section are propagated
to other sections as well unless they are overridden in the specific section itself. So for example if following `settings.toml`
file is used
```
[default]
db.mongodb_uri = "mongodb://localhost:27017"
logging.level = "DEBUG"

[dev-docker]
db.mongodb_uri = "mongodb://tesp-db:27017"
```
then dev-docker environment will use property `logging.level = DEBUG` as well, while property `db.mongodb_uri`
gets overridden to url of mongodb in the docker environment. `dev-docker` section in current [./settings.toml](https://github.com/CESNET/tesp-api/blob/main/settings.toml)
file is set up to support [./docker-compose.yaml](https://github.com/CESNET/tesp-api/blob/main/docker-compose.yaml) for development infrastructure.  
To apply different environment (i.e. to switch which section will be picked by `TESP API`) environment variable
`FASTAPI_PROFILE` must be set to the concrete name of such section (e.g. `FASTAPI_PROFILE=dev-docker` which can be seen
in the [./docker/tesp_api/Dockerfile](https://github.com/CESNET/tesp-api/blob/main/docker/tesp_api/Dockerfile))  

&nbsp;
### Authentication
`TESP API` can run without authentication (default). To enable Basic Auth, set `basic_auth.enable = true`
and configure `basic_auth.username` and `basic_auth.password` in `settings.toml`. To enable OAuth2,
set `oauth.enable = true` and pass a Bearer token; the token is validated via the issuer in its `iss`
claim using OIDC discovery.

Container execution runtime is controlled by the `CONTAINER_TYPE` environment variable (`docker` or
`singularity`). The default is `docker`.

&nbsp;
### S3 inputs and outputs
Inputs and outputs can be given as plain `s3://bucket/key` URLs, which is what most TES clients send (for example Nextflow's
TES executor with an S3 work dir). The S3 endpoint and credentials are configured on the server, never in the URL:

| Setting | Environment variable | Default | Meaning |
|---|---|---|---|
| `s3.endpoint_url` | `TESP_API_S3__ENDPOINT_URL` | empty (AWS S3) | Endpoint including the scheme, e.g. `https://s3.example.org` or `http://minio:9000` |
| `s3.region` | `TESP_API_S3__REGION` | `us-east-1` | Region used for signing |
| `s3.access_key_id` | `TESP_API_S3__ACCESS_KEY_ID` | empty | Access key. Empty means anonymous access (public buckets only) |
| `s3.secret_access_key` | `TESP_API_S3__SECRET_ACCESS_KEY` | empty | Secret key; must be set together with the access key |
| `s3.addressing_style` | `TESP_API_S3__ADDRESSING_STYLE` | `path` | `path` (`https://endpoint/bucket/key`, needed by most self-hosted stores), `virtual` (`https://bucket.endpoint/key`) or `auto` |
| `s3.presign_expiry_seconds` | `TESP_API_S3__PRESIGN_EXPIRY_SECONDS` | `3600` | Lifetime of the download URLs (1 s to 7 days) |
| `s3.output_presign_expiry_seconds` | `TESP_API_S3__OUTPUT_PRESIGN_EXPIRY_SECONDS` | `86400` | Lifetime of the upload URLs and POST policies (1 s to 7 days). Outputs are uploaded after the executors finish, so it must cover queueing plus the whole run of a task |

Put the keys in the environment or in `.secrets.toml` (git-ignored), not in `settings.toml`, e.g.
```
TESP_API_S3__ENDPOINT_URL=https://s3.example.org
TESP_API_S3__ACCESS_KEY_ID=...
TESP_API_S3__SECRET_ACCESS_KEY=...
```

How it works: when a task starts, `TESP API` turns each S3 input into a presigned `GET` URL (computed locally, no request
to S3) and the Pulsar staging container downloads it with `curl`. The secret key never leaves `TESP API`. The presigned
URLs are kept out of the job's shell trace (stored as the task's stderr) and out of the `TESP API` log: the job command
is logged with signatures, credentials and POST policies removed, Pulsar requests are logged by method and path only,
and Pulsar error messages are redacted the same way. botocore's debug logging, which would show the signature, is turned
off. CI fails when the compose logs contain presigned URLs (`ci/scan-logs.sh`) and then neither prints nor uploads them. They are part of the job command sent to Pulsar (as a query parameter of the
Pulsar REST call), so they can appear in Pulsar's and any proxy's logs until they expire. Keep
`s3.presign_expiry_seconds` no longer than the time tasks may wait in Pulsar before staging starts.

Outputs work the same way, except that the upload happens after the executors finished: a `FILE` output gets a
presigned `PUT` URL for its key, which the stage-out container uploads with `curl -T`. A `DIRECTORY` output gets a
presigned POST policy (S3 browser-based upload) whose only key condition is `starts-with` the output key plus `/`; the
stage-out container sends every file below the directory with its relative path appended, so `s3://bucket/work/results`
for `/data/results/sub/a.txt` becomes `s3://bucket/work/results/sub/a.txt`. A leaked upload URL or policy lets someone
write that one key, or keys below that prefix, until it expires (`s3.output_presign_expiry_seconds`); it gives no read
access and the secret key still never leaves `TESP API`.

Limitations:
- Legacy `s3://user:password@host:port/bucket/key` URLs (credentials or endpoint in the URL) are refused with
  `400 Bad Request`, so credentials are never stored with a task. Tasks stored by older versions keep their URLs; if
  such a task still has to run, its stage-in fails.
- Keys are used literally, without percent-decoding. URLs with a query (e.g. `?versionId=`) or fragment are refused.
- S3 `DIRECTORY` inputs are not supported yet and are refused with `400 Bad Request`.
- A `DIRECTORY` output needs a key prefix: `s3://bucket` and `s3://bucket/` are refused with `400 Bad Request`, as are
  `DIRECTORY` output keys containing `${filename}` (S3 POST uploads substitute it).
- Each output file is uploaded in a single request, so AWS S3 limits it to 5 GB.
- Any submitter can write to any key the server's credentials can write; there is no per-user bucket or prefix
  restriction.
- The upload URLs are created when the task starts running. A task that runs longer than
  `s3.output_presign_expiry_seconds` fails to upload its S3 outputs.
- The endpoint must be reachable from the Pulsar staging containers. For HTTPS endpoints with a private CA, the
  staging image must trust that CA.

### Task value restrictions
Executor values are passed to `docker run` / `singularity exec` literally (never expanded by the host shell).
Values that cannot be passed safely are refused with `400 Bad Request`, naming the field (e.g. `executors[0].env`):
- environment variable names must match `[A-Za-z_][A-Za-z0-9_]*`;
- images must not start with `-`;
- input paths, volumes and the directories of output paths must not contain `:` or `,`, which separate mount specs for
  Docker and Singularity.

With Singularity, environment variables are passed as `APPTAINERENV_<name>` and `SINGULARITYENV_<name>` rather than
with `--env`, which splits values on `,` and trims quotes, and `singularity exec` runs with `--no-eval` so values are
not shell-evaluated inside the container either. This needs Apptainer 1.0+ or SingularityCE 3.10+. Apptainer treats
the names `PATH`, `PREPEND_PATH` and `APPEND_PATH` specially and does not let tasks override `HOME` or `LD_LIBRARY_PATH`.

&nbsp;
### Configuring required services
You can have a look at [./docker-compose.yaml](https://github.com/CESNET/tesp-api/blob/main/docker-compose.yaml) to see how
the infrastructure for development should look like. Of course, you can configure those services in your preferred way if you are
going to start the project without `docker` or if you are trying to create other than `development` environment but some things
must remain as they are. For example, `TESP API` currently communicates with `Pulsar` via REST by default; configure Pulsar for
REST unless you set `pulsar.amqp_url` to enable AMQP.

&nbsp;
### Current Docker services
All the current `Docker` services which will be used when the project is started with `docker-compose` have common directory
[./docker](https://github.com/CESNET/tesp-api/tree/main/docker) for configurations, data, logs and Dockerfiles if required.
`docker-compose` should run out of the box, but sometimes it might happen that a problem with privileges occurs while for
example trying to create data folder for given service. Such issues should be resolved easily manually. Always look into
[./docker-compose.yaml](https://github.com/CESNET/tesp-api/blob/main/docker-compose.yaml) to see what directories need to mapped
which ports to be used etc. Following services are currently defined by [./docker-compose.yaml](https://github.com/CESNET/tesp-api/blob/main/docker-compose.yaml)
- **tesp-api** - This project itself. Depends on mongodb
- **tesp-db**  - [MongoDB](https://www.mongodb.com/) instance for persistence layer
- **pulsar_rest** - `Pulsar` configured to use REST API with access to a docker instance thanks to [DIND](https://hub.docker.com/_/docker) (enabled with `--profile pulsar`).
- **minio** - S3-compatible object store for the S3 smoke tests, on `172.17.0.1:9000` with test-only credentials (enabled with `--profile pulsar`). The `tesp-api` service is configured with this endpoint and these credentials (`TESP_API_S3__*`); change them when you use another object store.

If you want HTTP/FTP/S3 data transfer services for testing, use the separate
[docker/dts](docker/dts/README.md) compose stack.

&nbsp;
### Run the project
This project uses [Poetry](https://python-poetry.org/) for `dependency management` and `packaging`. `Poetry` makes it easy
to install libraries required by `TESP API`. It uses [./pyproject.toml](https://github.com/CESNET/tesp-api/blob/feature/TESP-0-github-proper-readme/pyproject.toml)
file to obtain current project orchestration configuration. `Poetry` automatically creates virtualenv, so it's easy to run
application immediately. You can use command `poetry config virtualenvs.in-project true` which **globally** configures
creation of virtualenv directories directly in the project instead of the default cache folder. Then all you need to do
to run `TESP API` deployed to `uvicorn` for example is:
```shell
poetry install
poetry run uvicorn tesp_api.tesp_api:app --reload --host localhost --port 8000
```
Otherwise, as was already mentioned, you can instead use `docker-compose` to start whole **development** infrastructure.
Service representing `TESP API` is configured to mount this project sources as a volume and `TESP API` is run with the very
same command as is mentioned above. Therefore, any changes made to the sources in this repository will be immediately applied to the docker
service as well, enabling live reloading which makes development within the `docker` environment very easy.
```shell
docker compose up -d
```
Or
```shell
docker compose --profile pulsar up -d
```

&nbsp;
## Exploring the functionality
`docker-compose` sets up whole development infrastructure. There will be two important endpoints to explore if you wish to
execute some `TES` tasks. Before doing any action, don't forget to run `docker-compose logs` command to see if each service
initialized properly or whether any errors occurred.

- **http://localhost:8080/** - will redirect to Swagger documentation of `TESP API`. This endpoint also currently acts as a frontend.
You can use it to execute REST based calls expected by the `TESP API`. Swagger is automatically generated from the sources,
and therefore it corresponds to the very current state of the `TESP API` interface.
- If you run the DTS stack from [docker/dts](docker/dts/README.md), MinIO console is available at
  **http://localhost:9001/** with `root` / `123456789` credentials.

### Executing simple TES task
This section will demonstrate execution of simple `TES` task which will calculate _[md5sum](https://en.wikipedia.org/wiki/Md5sum)_
hash of given input. There are more approaches of how I/O can be handled by `TES` but main goal here is to demonstrate `ftp server` as well.  

If you want to use the bundled HTTP/FTP/S3 services, start the DTS stack in [docker/dts](docker/dts/README.md)
and adapt hostnames/ports to match your network setup.

1. Upload a new file with your preferred name and content (e.g. name `holy_file` and content `Hello World!`) to your
FTP-backed storage. If you run the DTS stack, use the MinIO console at **http://localhost:9001/** to create a bucket
and upload the file. This file will be accessible through your FTP service and will be used as an input file for this
demonstration.
2. Go to **http://localhost:8080/** and use `POST /v1/tasks` request to create following `TES` task (task is sent in the request body).
In the `"inputs.url"` replace `<file_uploaded_to_storage>` with the file name you chose in the previous step. If http status of
returned response is 200, the response will contain `id` of created task in the response body which will be used to
reference this task later on.
```json
{
  "inputs": [
    {
      "url": "ftp://<ftp_host>:2121/<file_uploaded_to_storage>",
      "path": "/data/file1",
      "type": "FILE"
    }
  ],
  "outputs": [
    {
      "path": "/data/outfile",
      "url": "ftp://<ftp_host>:2121/outfile-1",
      "type": "FILE"
    }
  ],
  "executors": [
    {
      "image": "alpine",
      "command": [
        "md5sum"
      ],
      "stdin": "/data/file1",
      "stdout": "/data/outfile"
    }
  ]
}
```
3. Use `GET /v1/tasks/{id}` request to view task you have created. Use `id` from the response you have obtained in the
previous step. This request also supports `view` query parameter which can be used to limit the view of the task. By default,
`TESP API` will return `MINIMAL` view which only includes `id` and `state` of the requested task. Wait until task state is
set to the state `COMPLETE` or one of the error states. In case of an error state, depending on its type, the error will be part
of the task logs in the response (use `FULL` view), or you can inspect the logs of `TESP API` service, where error should be logged with respective
message.
4. Once the task completes you can check your FTP-backed storage (for the DTS stack, use the MinIO console at
**http://localhost:9001/**) where you should find uploaded `outfile-1` with output content of executed
_[md5sum](https://en.wikipedia.org/wiki/Md5sum)_. You can play around
by creating different tasks, just be sure to only use functionality which is currently supported - see [Known limitations](#known-limitations).
For example, you can omit `inputs.url` and instead use `inputs.content` which allows you to create input in place, or you can also
omit `outputs` and `executors.stdout` in which case the output will be present in the `logs.logs.stdout` as executor is no
longer configured to redirect stdout into the file.  

&nbsp;
### Known limitations of TESP API
| Domain   | Limitation                                                                                                                                                                         |
|----------|:-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| _Pulsar_ | `TESP API` should be able to dispatch executions to multiple `Pulsar` services via different types of `Pulsar` interfaces. Currently, only one `Pulsar` service is supported       |
| _Pulsar_ | `Pulsar` must be "polled" for job state. Preferably `Pulsar` should notify `TESP API` about state change. This is already default behavior when using `Pulsar` with message queues |
| _TES_    | Canceling a `TES` task calls Pulsar's cancel endpoint but container termination depends on Pulsar/runtime behavior. In-flight tasks may still complete.                             |
| _TES_    | Only `cpu_cores` and `ram_gb` are mapped to container runtime flags. Other resource fields (disk, preemptible, zones) are stored but not enforced.                                  |
| _TES_    | Task `tags` are accepted and stored but not used by the scheduler or runtime.                                                                                                      |
| _TES_    | Task `logs.outputs` is not populated. Use `outputs` to persist result files.                                                                                                       |
| _TES_    | Outputs whose path does not exist after the executors finish are skipped (noted in the task's stderr) and the task can still be `COMPLETE`. TES has no `optional` flag for outputs; clients such as Nextflow check for missing outputs themselves. Outputs are not uploaded when an executor fails. |
| _TES_    | Outputs are uploaded to `s3://` URLs and to HTTP(S) upload endpoints (multipart `POST`, form field `file`). Other URLs, such as the `ftp://` output in the example above, get the same `curl` multipart `POST`, which is not an FTP upload, so FTP outputs do not work. |
| _TES_    | `DIRECTORY` outputs upload every regular file below the directory, keeping relative paths: below the URL for S3, and as the multipart file name (e.g. `sub/a.txt`) for HTTP(S) upload endpoints, which receive one `POST` per file (form field `file`). Symbolic links inside the directory are not uploaded (each is noted in the task's stderr). |
| _TES_    | Output paths must be absolute. Wildcards (`*`, `?`; TES 1.1 `path_prefix` outputs) are not supported and are refused with `400 Bad Request`. |
| _TES_    | When an executor sets `stdin`, `stdout` or `stderr`, its image must provide `sh`. `logs.logs.stdout`/`stderr` then hold the last 64 KiB of each redirected stream, filtered to printable ASCII, so non-ASCII text is dropped from the log (the file keeps the exact bytes); without `tail`/`tr` in the image the log copy is empty. |

&nbsp;

History note: _The original intention of this project was to modify the `Pulsar` project so its Rest API would be compatible with the `TES` standard.
Later a decision was made that rather a separate microservice will be created, decoupled from the `Pulsar`, implementing the `TES`
standard and distributing `TES` tasks execution to `Pulsar` applications._