from typing import Optional


def job_script(*commands: Optional[str]) -> Optional[str]:
    """
    Joins the job's stage commands into the script submitted to Pulsar, or returns ``None`` when there is nothing to
    run. ``commands`` are the stage-in command, the executor commands in order, then the stage-out command; an empty
    one is skipped.

    A failing stage-in ends the job with its exit code. The executors run in order until one fails. Stage-out runs
    either way, so the outputs a failed executor left (e.g. Nextflow's ``.exitcode`` and ``.command.err``) are still
    uploaded (issue #30). The job then exits with the failed executor's code, so the task still ends in
    EXECUTOR_ERROR, or else with the stage-out code.

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

    script = ["set -e"]
    if stage_in:
        # Explicit: set -e ignores a failure left of the last && in a list such as "mkdir -p ... && docker run".
        script.append("{ " + stage_in + "; } || exit $?")
    script.append("rc=0")
    if executors:
        # The failed executor's code is kept for the end.
        script.append("{ " + " && ".join(executors) + "; } || rc=$?")
    if stage_out:
        script.append("{ " + stage_out + "; } || { s=$?; [ $rc -ne 0 ] || rc=$s; }")
    script.append("exit $rc")
    return "; ".join(script)
