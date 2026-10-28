from typing import Optional

from tesp_api.utils.container import ASCII_TEXT_FILTER, STDIO_LOG_TAIL_BYTES

# The job's streams are captured here, in Pulsar's job working directory (its job script runs `cd $working_directory`
# first), and only a filtered tail is passed on to Pulsar.
JOB_STDOUT_FILE = ".tesp-job-stdout"
JOB_STDERR_FILE = ".tesp-job-stderr"


def job_script(*commands: Optional[str]) -> Optional[str]:
    """
    Joins the job's stage commands (stage-in, executors, stage-out) into the script submitted to Pulsar, or returns
    ``None`` when there is nothing to run. The stages are chained with ``&&``, so the first failing one ends the job
    with its exit code (``set -e`` is kept only as a guard; stages must stay ``&&``-joined).

    The script is deliberately not traced (no ``set -x``): Pulsar returns the job's stderr, which is stored as the
    task's ``ExecutorLog.stderr``, and a trace would put executor env values, presigned URLs and host paths there.

    Pulsar decodes the job's stdout and stderr as strict UTF-8 when reporting the job status, so binary output would
    break the status request (issue #20). The stages' streams therefore go to files, and only their last
    ``STDIO_LOG_TAIL_BYTES``, reduced to printable ASCII, tab and line breaks, reach Pulsar.
    """
    parts = [c.strip() for c in commands if c and c.strip()]
    if not parts:
        return None
    stages = " && ".join(["set -e", *parts])
    return "; ".join([
        f"( {stages} ) >{JOB_STDOUT_FILE} 2>{JOB_STDERR_FILE}",
        "rc=$?",
        f"tail -c {STDIO_LOG_TAIL_BYTES} {JOB_STDOUT_FILE} 2>/dev/null | {ASCII_TEXT_FILTER} 2>/dev/null",
        # >&2 before 2>/dev/null: the copy goes to the job's stderr, tr's own errors are dropped
        f"tail -c {STDIO_LOG_TAIL_BYTES} {JOB_STDERR_FILE} 2>/dev/null | {ASCII_TEXT_FILTER} >&2 2>/dev/null",
        f"rm -f {JOB_STDOUT_FILE} {JOB_STDERR_FILE}",
        "exit $rc",
    ])
