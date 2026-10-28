import re
from datetime import datetime, timezone
from typing import List, Optional, Tuple

# Marker files the job script writes into its working directory, the directory Pulsar's job script `cd`s into before
# running the command line. Pulsar lists that directory in the completed job's status (`working_directory_contents`),
# so tesp-api can tell how far the job got without a further request. Executors cannot write there: it is not mounted
# into their containers.
JOB_STARTED_FILE = "tesp-job-started"
STAGE_FAILED_FILES = {"stage-in": "tesp-stage-in-failed", "stage-out": "tesp-stage-out-failed"}

# Each executor's stdout, stderr and "<start> <end> <exit code>" (epoch seconds) go to their own files there, so every
# executor gets its own ExecutorLog without the stage-in and stage-out output (issue #67). Only the last
# EXECUTOR_LOG_TAIL_BYTES of each stream are kept (TES: "no guarantees are made about how much of the stream is
# captured"). The status file is written last: when it exists, the stream files are complete.
EXECUTOR_FILE = "tesp-executor-{index}.{kind}"
EXECUTOR_LOG_TAIL_BYTES = 64 * 1024

# Lines and characters of the job's stderr quoted in the system log of a failed stage
SYSTEM_LOG_TAIL_LINES = 20
SYSTEM_LOG_TAIL_CHARS = 4096


def executor_file(index: int, kind: str) -> str:
    """Name of executor ``index``'s ``stdout``, ``stderr`` or ``status`` file in the job's working directory."""
    return EXECUTOR_FILE.format(index=index, kind=kind)


# Shell function the job script defines once, called right after each executor with its number: keeps the tail of its
# stream files, writes its status file and returns its exit code.
_EXECUTOR_END = (
    "tesp_executor_end() { rc=$?; for f in " + EXECUTOR_FILE.format(index="$1", kind="stdout") + " "
    + EXECUTOR_FILE.format(index="$1", kind="stderr") + f"; do tail -c {EXECUTOR_LOG_TAIL_BYTES} $f > $f.tail; "
    "mv -f $f.tail $f; done; echo \"$start $(date +%s) $rc\" > " + EXECUTOR_FILE.format(index="$1", kind="status")
    + "; return $rc; }"
)


def _executor(index: int, command: str) -> str:
    out, err = executor_file(index, "stdout"), executor_file(index, "stderr")
    return f"{{ start=$(date +%s); ( {command} ) >{out} 2>{err}; tesp_executor_end {index}; }}"


def job_script(*commands: Optional[str]) -> Optional[str]:
    """
    Joins the job's stage commands into the script submitted to Pulsar, or returns ``None`` when there is nothing to
    run. ``commands`` are the stage-in command, the executor commands in order, then the stage-out command; an empty
    one is skipped. The first failing stage ends the job with its exit code. A failing stage-in or stage-out also
    leaves a marker file (``STAGE_FAILED_FILES``), so the task can end in SYSTEM_ERROR rather than EXECUTOR_ERROR
    (issue #66, see ``failed_stage``). Each executor's streams and exit code go to files (``executor_file``); the
    job's own stdout and stderr hold only the stage-in and stage-out output.

    The script runs in a subshell. Pulsar appends ``; return_code=$?; echo $return_code > <job dir>/return_code`` to
    the command line, in the same shell; an ``exit`` outside a subshell would end that shell before the exit code is
    written, and the executor's exit code would be lost (issue #64).

    The script is deliberately not traced (no ``set -x``): Pulsar returns the job's stderr, which is stored as the
    task's ``ExecutorLog.stderr``, and a trace would put executor env values, presigned URLs and host paths there.
    """
    def present(command: Optional[str]) -> Optional[str]:
        return command.strip() if command and command.strip() else None

    stage_in = present(commands[0]) if commands else None
    stage_out = present(commands[-1]) if len(commands) > 1 else None
    # Executor numbers are their positions in the task, so an empty command (if any) keeps its number.
    executors = [(i, c) for i, c in enumerate(map(present, commands[1:-1])) if c]
    if not (stage_in or executors or stage_out):
        return None

    def stage(command: str, name: str) -> str:
        # A stage command may itself be a list, e.g. "mkdir -p ... && docker run ...", hence its own subshell.
        return f"( {command} ) || {{ rc=$?; : > {STAGE_FAILED_FILES[name]}; exit $rc; }}"

    script: List[str] = [f": > {JOB_STARTED_FILE}"]
    if stage_in:
        script.append(stage(stage_in, "stage-in"))
    if executors:
        script.append(_EXECUTOR_END)
        script.append(" && ".join(_executor(i, c) for i, c in executors) + " || exit $?")
    if stage_out:
        script.append(stage(stage_out, "stage-out"))
    return "( " + "; ".join(script) + " )"


def failed_stage(status: dict) -> Optional[str]:
    """
    Which part of the job failed outside the executors, from a completed Pulsar job status: ``"stage-in"``,
    ``"stage-out"``, ``"submission"`` (Pulsar or the batch system never started the job script, e.g. SLURM rejected
    the job's resources), or ``None`` (no such failure, or a Pulsar too old to list the working directory).
    """
    if 'working_directory_contents' not in status:
        # Pulsar answers a job that failed before launch, and whose details it cannot read, with the status only.
        return "submission" if status.get('status') == 'failed' and set(status) <= {
            'job_id', 'complete', 'status', 'returncode'} else None
    contents = set(status.get('working_directory_contents') or [])
    for name, marker in STAGE_FAILED_FILES.items():
        if marker in contents:
            return name
    if status.get('status') == 'failed' and JOB_STARTED_FILE not in contents:
        return "submission"
    return None


def parse_executor_status(content: bytes) -> Tuple[datetime, datetime, int]:
    """Start time, end time and exit code from an executor's status file (see ``EXECUTOR_FILE``)."""
    start, end, exit_code = content.decode().split()
    return (datetime.fromtimestamp(int(start), timezone.utc), datetime.fromtimestamp(int(end), timezone.utc),
            int(exit_code))


def stream_text(content: Optional[bytes]) -> str:
    """An executor's stream file as text: its tail, undecodable bytes replaced (a tail can start inside a character)."""
    return (content or b"")[-EXECUTOR_LOG_TAIL_BYTES:].decode("utf-8", errors="replace")


def tesp_api_notes(stderr: str) -> List[str]:
    """The notes the stage scripts write to the job's stderr, e.g. on an output that was not found and skipped."""
    return [line.strip() for line in (stderr or "").splitlines() if line.startswith("tesp-api: ")]


def _tail(text: str) -> str:
    # \r too: curl's progress meter rewrites its line with carriage returns.
    lines = [line.rstrip() for line in re.split(r"\r\n|\r|\n", text or "") if line.strip()]
    return "\n".join(lines[-SYSTEM_LOG_TAIL_LINES:])[-SYSTEM_LOG_TAIL_CHARS:]


def stage_failure_log(stage: str, returncode, stderr: str) -> str:
    """The system log entry of a task whose ``stage`` (see ``failed_stage``) failed; ``stderr`` is the job's."""
    reason = {
        "stage-in": f"stage-in failed (exit code {returncode}): downloading the inputs failed",
        "stage-out": f"stage-out failed (exit code {returncode}): uploading the outputs failed",
        "submission": "the job did not start: Pulsar or the batch system refused it (for example, resources the "
                      "batch system cannot provide); Pulsar's log has the details",
    }[stage]
    tail = _tail(stderr)
    return f"{reason}\n{tail}" if tail else reason
