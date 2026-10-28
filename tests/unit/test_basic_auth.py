"""Basic auth must answer bad or missing credentials with 401, never 500 (issue #51)."""
import base64

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPBasicCredentials
from fastapi.testclient import TestClient

from tesp_api.api.endpoints.endpoint_utils import parse_verify_token
from tesp_api.config.properties import properties

USERNAME = "user"
PASSWORD = "password"
NON_ASCII_PASSWORD = "pässwörd-✓"

app = FastAPI()


@app.get("/protected")
def protected(user=Depends(parse_verify_token)):
    return {"user": user}


client = TestClient(app)


@pytest.fixture
def basic_auth(monkeypatch):
    def configure(password=PASSWORD):
        monkeypatch.setattr(properties.basic_auth, "enable", True)
        monkeypatch.setattr(properties.basic_auth, "username", USERNAME)
        monkeypatch.setattr(properties.basic_auth, "password", password)
    configure()
    return configure


def basic_header(username, password):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def verify(username, password):
    return parse_verify_token(token=None, basic_credentials=HTTPBasicCredentials(username=username, password=password))


def test_missing_header_gives_401_with_challenge(basic_auth):
    response = client.get("/protected")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Basic"


def test_wrong_password_gives_401(basic_auth):
    response = client.get("/protected", headers=basic_header(USERNAME, "wrong"))
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Basic"


def test_correct_password_gives_200(basic_auth):
    response = client.get("/protected", headers=basic_header(USERNAME, PASSWORD))
    assert response.status_code == 200
    assert response.json() == {"user": USERNAME}


def test_ascii_password_against_non_ascii_configured_gives_401(basic_auth):
    basic_auth(NON_ASCII_PASSWORD)
    response = client.get("/protected", headers=basic_header(USERNAME, PASSWORD))
    assert response.status_code == 401


# FastAPI's HTTPBasic decodes the header as ASCII, so non-ASCII credentials
# cannot arrive over HTTP; check the comparison directly.
def test_non_ascii_wrong_password_gives_401(basic_auth):
    with pytest.raises(HTTPException) as e:
        verify(USERNAME, NON_ASCII_PASSWORD)
    assert e.value.status_code == 401
    assert e.value.headers == {"WWW-Authenticate": "Basic"}


def test_non_ascii_configured_password_is_accepted(basic_auth):
    basic_auth(NON_ASCII_PASSWORD)
    assert verify(USERNAME, NON_ASCII_PASSWORD) == USERNAME


def test_disabled_basic_auth_lets_requests_through(monkeypatch):
    monkeypatch.setattr(properties.basic_auth, "enable", False)
    monkeypatch.setattr(properties.oauth, "enable", False)
    assert client.get("/protected").status_code == 200
