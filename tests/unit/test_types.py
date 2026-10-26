import pydantic
import pytest

from tesp_api.utils.types import HttpUrl


class _Model(pydantic.BaseModel):
    url: HttpUrl


def test_http_url_parses():
    url = _Model(url="http://example.org:8080/path").url
    assert url.scheme == "http"
    assert url.host == "example.org"
    assert url.port == "8080"


def test_http_url_rejects_other_scheme():
    with pytest.raises(pydantic.ValidationError):
        _Model(url="ftp://example.org")
