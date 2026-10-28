import http.server
import shlex
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from tesp_api.repository.model.task import TesTaskExecutor, TesTaskIOType
from tesp_api.utils.container import MAX_REDIRECTS, _file_download, stage_in_command

STAGE_EXEC = TesTaskExecutor(image="curl-image", command=[], workdir=Path("/downloads"))


@pytest.mark.parametrize("container_type", ["docker", "singularity"])
def test_no_stage_in_when_all_inputs_are_inline(container_type):
    # Inline `content` inputs are uploaded to Pulsar directly; there is nothing to download,
    # and a container started without a command fails (singularity exec) or runs the image default.
    input_confs = [{'container_path': '/data/in', 'pulsar_path': '/staging/inputs/input_file_0',
                    'url': None, 'type': TesTaskIOType.FILE}]
    assert stage_in_command(STAGE_EXEC, {}, "/staging/inputs", input_confs, container_type) == ""


@pytest.mark.parametrize("container_type", ["docker", "singularity"])
def test_stage_in_downloads_url_inputs(container_type):
    input_confs = [{'container_path': '/data/in', 'pulsar_path': '/staging/inputs/input_file_0',
                    'url': 'http://example.org/in.txt', 'type': TesTaskIOType.FILE}]
    cmd = stage_in_command(STAGE_EXEC, {}, "/staging/inputs", input_confs, container_type)
    assert "-o input_file_0 http://example.org/in.txt" in cmd


# Issue #41: without -L curl saved a redirect's body as the input and exited 0.

CONTENT = b"the real input\n"


class _RedirectingHandler(http.server.BaseHTTPRequestHandler):
    """/file serves CONTENT; /redirect/<path> answers 302 to /<path>; /loop redirects to itself."""

    def do_GET(self):
        if self.path == "/file":
            self.send_response(200)
            self.send_header("Content-Length", str(len(CONTENT)))
            self.end_headers()
            self.wfile.write(CONTENT)
        elif self.path.startswith("/redirect/"):
            self._redirect("/" + self.path[len("/redirect/"):])
        elif self.path == "/loop":
            self._redirect("/loop")
        else:
            self.send_error(404)

    def _redirect(self, location):
        body = b"<html>Redirecting</html>"
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def http_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RedirectingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


needs_curl = pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl")


def _download(url, tmp_path):
    result = subprocess.run(["sh", "-c", _file_download(url, "input_file_0")], cwd=tmp_path,
                            capture_output=True, text=True)
    return result, tmp_path / "input_file_0"


@pytest.mark.parametrize("container_type", ["docker", "singularity"])
def test_stage_in_follows_redirects(container_type):
    input_confs = [{'container_path': '/data/in', 'pulsar_path': '/staging/inputs/input_file_0',
                    'url': 'https://example.org/in.txt', 'type': TesTaskIOType.FILE}]
    cmd = stage_in_command(STAGE_EXEC, {}, "/staging/inputs", input_confs, container_type)
    assert "curl -fgL " in cmd
    assert f"--max-redirs {MAX_REDIRECTS}" in cmd


def test_https_input_may_not_redirect_to_http():
    assert "--proto-redir =https " in _file_download("https://example.org/in.txt", "f")
    assert "--proto-redir =https " in _file_download("HTTPS://example.org/in.txt", "f")
    assert "--proto-redir =http,https " in _file_download("http://example.org/in.txt", "f")


def test_file_download_keeps_quoting_and_globoff():
    cmd = _file_download("http://example.org/a{1,2}[1-2].txt?x=1&y='2'", "in put")
    argv = shlex.split(cmd)
    assert argv[-3:] == ["-o", "in put", "http://example.org/a{1,2}[1-2].txt?x=1&y='2'"]
    assert "-fgL" in argv


@needs_curl
def test_download_follows_redirect_to_content(http_server, tmp_path):
    result, path = _download(f"{http_server}/redirect/redirect/file", tmp_path)
    assert result.returncode == 0, result.stderr
    assert path.read_bytes() == CONTENT


@needs_curl
def test_download_fails_on_redirect_loop(http_server, tmp_path):
    result, _ = _download(f"{http_server}/loop", tmp_path)
    assert result.returncode != 0


@needs_curl
def test_download_fails_on_redirect_to_missing_file(http_server, tmp_path):
    result, _ = _download(f"{http_server}/redirect/missing", tmp_path)
    assert result.returncode != 0


@pytest.mark.parametrize("container_type", ["docker", "singularity"])
def test_directory_download_stays_on_host(container_type):
    input_confs = [{'container_path': '/data/in', 'pulsar_path': '/staging/inputs/input_dir_0',
                    'url': 'http://example.org/dir/', 'type': TesTaskIOType.DIRECTORY}]
    cmd = stage_in_command(STAGE_EXEC, {}, "/staging/inputs", input_confs, container_type)
    assert "wget " in cmd and "--no-parent" in cmd
    assert "--span-hosts" not in cmd and " -H " not in cmd
    assert f"--max-redirect={MAX_REDIRECTS}" in cmd
