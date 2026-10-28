import re
from typing import List, Optional

# Marker files the job script writes into its working directory, the directory Pulsar's job script `cd`s into before
# running the command line. Pulsar lists that directory in the completed job's status (`working_directory_contents`),
# so tesp-api can tell how far the job got without a further request. Executors cannot write there: it is not mounted
# into their containers.
JOB_STARTED_FILE = "tesp-job-started"
STAGE_FAILED_FILES = {"stage-in": "tesp-stage-in-failed", "stage-out": "tesp-stage-out-failed"}

# Lines and characters of the job's stderr quoted in the system log of a failed stage
SYSTEM_LOG_TAIL_LINES = 20
SYSTEM_LOG_TAIL_CHARS = 4096


def job_script(*commands: Optional[str]) -> Optional[str]:
    """
    Joins the job's stage commands into the script submitted to Pulsar, or returns ``None`` when there is nothing to
    run. ``commands`` are the stage-in command, the executor commands in order, then the stage-out command; an empty
    one is skipped. The first failing stage ends the job with its exit code. A failing stage-in or stage-out also
    leaves a marker file (``STAGE_FAILED_FILES``), so the task can end in SYSTEM_ERROR rather than EXECUTOR_ERROR
    (issue #66, see ``failed_stage``).

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
    executors = [c for c in map(present, commands[1:-1]) if c]
    if not (stage_in or executors or stage_out):
        return None

    def stage(command: str, name: str) -> str:
        # A stage command may itself be a list, e.g. "mkdir -p ... && docker run ...", hence its own subshell.
        return f"( {command} ) || {{ rc=$?; : > {STAGE_FAILED_FILES[name]}; exit $rc; }}"

    script: List[str] = [f": > {JOB_STARTED_FILE}"]
    if stage_in:
        script.append(stage(stage_in, "stage-in"))
    if executors:
        script.append(" && ".join(f"( {c} )" for c in executors) + " || exit $?")
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
