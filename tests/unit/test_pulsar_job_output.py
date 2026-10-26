"""Pulsar >= 0.15 reports the job command's own streams as `job_stdout`/`job_stderr`; `stdout`/`stderr`
are Galaxy's tool_stdout/tool_stderr metadata files, which a plain command line never writes."""
from tesp_api.service.pulsar_operations import job_output


def test_new_pulsar_reports_job_streams():
    status = {'stdout': '', 'stderr': '', 'job_stdout': 'out\n', 'job_stderr': 'err\n'}
    assert job_output(status) == ('out\n', 'err\n')


def test_old_pulsar_reports_stdout_stderr():
    assert job_output({'stdout': 'out\n', 'stderr': 'err\n'}) == ('out\n', 'err\n')


def test_job_streams_win_when_both_set():
    status = {'stdout': 'tool\n', 'stderr': 'tool\n', 'job_stdout': 'job\n', 'job_stderr': 'job\n'}
    assert job_output(status) == ('job\n', 'job\n')


def test_missing_or_null_streams_are_empty():
    assert job_output({'stdout': None, 'stderr': None, 'job_stdout': None}) == ('', '')
    assert job_output({}) == ('', '')
