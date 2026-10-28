import re

from tesp_api.repository.model.task import TesTaskOutput
from tesp_api.utils.container import map_volumes, volume_name

DOCKER_VOLUME_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+")


def _output(path):
    return TesTaskOutput(path=path, url="http://example.org/upload", type="FILE")


def _names(volume_confs):
    return {conf['container_path']: conf['volume_name'] for conf in volume_confs}


def test_paths_differing_only_by_slashes_get_different_volumes():
    _, volume_confs = map_volumes("job", ["/a/b", "/ab", "/a/b_", "/a_b"], [])
    assert len(set(_names(volume_confs).values())) == 4


def test_output_directory_and_volume_paths_do_not_collide():
    output_confs, volume_confs = map_volumes("job", ["/ab"], [_output("/a/b/out.txt")])
    names = _names(volume_confs)
    assert names.keys() == {"/a/b", "/ab"}
    assert names["/a/b"] != names["/ab"]
    assert output_confs[0]['volume_name'] == names["/a/b"]


def test_same_path_shares_one_volume():
    output_confs, volume_confs = map_volumes("job", ["/data"], [_output("/data/a"), _output("/data/b")])
    assert volume_confs == [{'volume_name': volume_name("job", "/data"), 'container_path': "/data"}]
    assert {conf['volume_name'] for conf in output_confs} == {volume_name("job", "/data")}


def test_volume_names_are_per_job_and_valid_docker_names():
    for path in ["/data", "/a b/ü", "/x.y-z", "/"]:
        name = volume_name("6543210fedcba", path)
        assert name.startswith("vol-6543210fedcba-")
        assert DOCKER_VOLUME_NAME.fullmatch(name)
        assert name != volume_name("other", path)
