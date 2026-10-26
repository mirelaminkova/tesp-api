"""Unit tests for Executor.stdin/stdout/stderr handling in container command construction.

TES v1.0 `tesExecutor`: `stdin` is a container path piped to the command's stdin, `stdout`/`stderr`
are container paths the streams are written to. `tesExecutorLog.stdout`/`stderr` are a
convenience copy of the streams with no guarantee on how much is captured.

The tests take the argv the builder places after the image (i.e. what runs inside the container)
and execute it with the host's `sh`, using temporary files in place of container paths.
"""
import os
import shlex
import signal
import subprocess
import time

import pytest
from pymonad.maybe import Just, Nothing

from tesp_api.repository.model.task import TesTaskExecutor
from tesp_api.utils.container import ContainerCommandBuilder, STDIO_LOG_TAIL_BYTES, run_command

IMAGE = "stdio-test-image"


def _container_argv(run_cmd: str, image: str = IMAGE):
    tokens = shlex.split(run_cmd)
    return tokens[tokens.index(image) + 1:]


def _build(command, stdin=Nothing, stdout=Nothing, stderr=Nothing, container_type="docker"):
    builder = ContainerCommandBuilder(container_type) \
        .with_image(IMAGE) \
        .with_command(command, stdin, stdout, stderr)
    image = "docker://" + IMAGE if container_type == "singularity" else IMAGE
    return _container_argv(builder.get_run_command(), image)


def _run(argv, cwd=None):
    return subprocess.run(argv, capture_output=True, text=True, timeout=10, cwd=cwd)


def test_command_without_streams_is_unchanged():
    assert _build(["echo", "hello"]) == ["echo", "hello"]


def test_stdout_is_written_to_file_and_copied_to_log(tmp_path):
    out = tmp_path / "stdout.txt"
    result = _run(_build(["echo", "hello"], stdout=Just(str(out))))

    assert result.returncode == 0
    assert out.read_text() == "hello\n"
    assert result.stdout == "hello\n"


def test_stderr_is_written_to_file_and_copied_to_log(tmp_path):
    err = tmp_path / "stderr.txt"
    result = _run(_build(["sh", "-c", "echo oops >&2"], stderr=Just(str(err))))

    assert result.returncode == 0
    assert err.read_text() == "oops\n"
    assert result.stderr == "oops\n"
    assert result.stdout == ""


def test_stdin_is_piped_from_file(tmp_path):
    src = tmp_path / "stdin.txt"
    src.write_text("abc\n")
    result = _run(_build(["tr", "a-z", "A-Z"], stdin=Just(str(src))))

    assert result.returncode == 0
    assert result.stdout == "ABC\n"


def test_all_streams_together(tmp_path):
    src, out, err = tmp_path / "in", tmp_path / "out", tmp_path / "err"
    src.write_text("data\n")
    result = _run(_build(["sh", "-c", "cat; echo warn >&2"],
                         Just(str(src)), Just(str(out)), Just(str(err))))

    assert result.returncode == 0
    assert out.read_text() == "data\n"
    assert err.read_text() == "warn\n"
    assert result.stdout == "data\n"
    assert result.stderr == "warn\n"


def test_exit_code_is_preserved(tmp_path):
    out = tmp_path / "out"
    result = _run(_build(["sh", "-c", "echo partial; exit 3"], stdout=Just(str(out))))

    assert result.returncode == 3
    assert out.read_text() == "partial\n"


def test_exit_code_is_preserved_with_stderr(tmp_path):
    err = tmp_path / "err"
    result = _run(_build(["sh", "-c", "echo bad >&2; exit 5"], stderr=Just(str(err))))

    assert result.returncode == 5
    assert err.read_text() == "bad\n"
    assert result.stderr == "bad\n"


def test_empty_paths_are_ignored():
    assert _build(["echo", "hello"], Just(""), Just(""), Just("")) == ["echo", "hello"]


def test_same_file_for_stdout_and_stderr(tmp_path):
    log = tmp_path / "log"
    result = _run(_build(["sh", "-c", "echo out; echo err >&2"], stdout=Just(str(log)), stderr=Just(str(log))))

    assert result.returncode == 0
    assert log.read_text() == "out\nerr\n"
    assert result.stdout == "out\nerr\n"
    assert result.stderr == ""


def test_path_starting_with_dash(tmp_path):
    result = _run(_build(["echo", "hi"], stdout=Just("-n")), cwd=tmp_path)

    assert result.returncode == 0
    assert (tmp_path / "-n").read_text() == "hi\n"
    assert result.stdout == "hi\n"


def test_sigterm_is_forwarded_to_command(tmp_path):
    proc = subprocess.Popen(_build(["sleep", "30"], stdout=Just(str(tmp_path / "out"))))
    try:
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)
        # sh exits with the child's status (128 + SIGTERM) instead of being killed itself
        assert proc.wait(timeout=5) == 128 + signal.SIGTERM
    finally:
        proc.kill()


def test_shell_wrapped_command_redirects_whole_script(tmp_path):
    out = tmp_path / "out"
    result = _run(_build(["sh", "-c", "echo a; echo b"], stdout=Just(str(out))))

    assert result.returncode == 0
    assert out.read_text() == "a\nb\n"


def test_arguments_are_not_interpreted_by_shell(tmp_path):
    out = tmp_path / "out"
    result = _run(_build(["echo", "$HOME", ">", "x;", "`id`"], stdout=Just(str(out))))

    assert result.returncode == 0
    assert out.read_text() == "$HOME > x; `id`\n"
    assert not (tmp_path / "x;").exists()


def test_paths_with_special_characters(tmp_path):
    out_dir = tmp_path / "dir with space"
    out_dir.mkdir()
    out = out_dir / "o'ut$x"
    result = _run(_build(["echo", "hi"], stdout=Just(str(out))))

    assert result.returncode == 0
    assert out.read_text() == "hi\n"


def test_log_copy_is_bounded(tmp_path):
    out = tmp_path / "out"
    size = STDIO_LOG_TAIL_BYTES * 3
    result = _run(_build(["sh", "-c", f"yes | head -c {size}"], stdout=Just(str(out))))

    assert result.returncode == 0
    assert out.stat().st_size == size
    assert len(result.stdout) == STDIO_LOG_TAIL_BYTES


def test_log_copy_is_ascii_text_only(tmp_path):
    # Pulsar decodes job stdout/stderr as strict UTF-8; binary data or a tail that starts mid
    # multi-byte character must not reach it. The file itself keeps the exact bytes.
    # Calls subprocess directly to compare raw bytes rather than decoded text.
    out, err = tmp_path / "out", tmp_path / "err"
    script = r"printf 'a\377\000b\303\251\n'; printf 'x\200y\n' >&2"
    result = subprocess.run(_build(["sh", "-c", script], stdout=Just(str(out)), stderr=Just(str(err))),
                            capture_output=True, timeout=10)

    assert result.returncode == 0
    assert out.read_bytes() == b"a\xff\x00b\xc3\xa9\n"
    assert result.stdout == b"ab\n"
    assert result.stderr == b"xy\n"


@pytest.mark.parametrize("container_type", ["docker", "singularity"])
def test_container_types_use_same_inner_command(tmp_path, container_type):
    out = tmp_path / "out"
    result = _run(_build(["echo", "hello"], stdout=Just(str(out)), container_type=container_type))

    assert result.returncode == 0
    assert out.read_text() == "hello\n"


def test_run_command_passes_executor_streams(tmp_path):
    src, out = tmp_path / "in", tmp_path / "out"
    src.write_text("xyz\n")
    executor = TesTaskExecutor(image=IMAGE, command=["tr", "a-z", "A-Z"], stdin=str(src), stdout=str(out))
    cmd = run_command(executor=executor, job_id="job", resource_conf={}, volume_confs=[], input_confs=[],
                      output_confs=[], inputs_directory="", container_type="docker")

    result = _run(_container_argv(cmd))
    assert result.returncode == 0
    assert out.read_text() == "XYZ\n"
