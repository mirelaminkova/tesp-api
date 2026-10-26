from typing import Optional


def job_script(*commands: Optional[str]) -> Optional[str]:
    """
    Joins the job's stage commands (stage-in, executors, stage-out) into the script submitted to Pulsar, or returns
    ``None`` when there is nothing to run. The stages are chained with ``&&``, so the first failing one ends the job
    with its exit code (``set -e`` is kept only as a guard; stages must stay ``&&``-joined).

    The script is deliberately not traced (no ``set -x``): Pulsar returns the job's stderr, which is stored as the
    task's ``ExecutorLog.stderr``, and a trace would put executor env values, presigned URLs and host paths there.
    """
    parts = [c.strip() for c in commands if c and c.strip()]
    return " && ".join(["set -e", *parts]) if parts else None
