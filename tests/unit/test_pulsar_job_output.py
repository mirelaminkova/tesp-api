"""Pulsar >= 0.15 reports the job command's own streams as `job_stdout`/`job_stderr`; `stdout`/`stderr`
are Galaxy's tool_stdout/tool_stderr metadata files, which a plain command line never writes."""
from tesp_api.service.pulsar_operations import job_streams, MAX_JOB_STREAM_CHARS


def test_new_pulsar_reports_job_streams():
    status = {'stdout': '', 'stderr': '', 'job_stdout': 'out\n', 'job_stderr': 'err\n'}
    assert job_streams(status) == ('out\n', 'err\n')


def test_old_pulsar_reports_stdout_stderr():
    assert job_streams({'stdout': 'out\n', 'stderr': 'err\n'}) == ('out\n', 'err\n')


def test_job_streams_win_when_both_set():
    status = {'stdout': 'tool\n', 'stderr': 'tool\n', 'job_stdout': 'job\n', 'job_stderr': 'job\n'}
    assert job_streams(status) == ('job\n', 'job\n')


def test_missing_or_null_streams_are_empty():
    assert job_streams({'stdout': None, 'stderr': None, 'job_stdout': None}) == ('', '')
    assert job_streams({}) == ('', '')


def test_streams_keep_only_the_tail():
    # Pulsar returns job_stdout/job_stderr untruncated; the task document must stay small.
    big = "x" * MAX_JOB_STREAM_CHARS + "end\n"
    stdout, stderr = job_streams({'job_stdout': big, 'job_stderr': big})
    assert len(stdout) == len(stderr) == MAX_JOB_STREAM_CHARS
    assert stdout.endswith("end\n")
