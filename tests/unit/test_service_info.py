"""GET /v1/service-info from settings, built once (issue #69).

It used to return the upstream deployment's values (Masaryk University, `dev`) on every deployment, and a version read
by running ci/version.sh on each request, trailing newline included.
"""
import importlib
import os
import sys
import types

import pydantic
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tesp_api.api import service_info
from tesp_api.config.properties import get_properties
from tesp_api.utils.commons import Commons

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _changelog_version():
    with open(os.path.join(REPO_ROOT, 'CHANGELOG.md')) as changelog:
        for line in changelog:
            if line.startswith('## [') and 'unreleased' not in line.lower():
                return line.split('[', 1)[1].split(']', 1)[0]


@pytest.fixture
def fresh_version():
    Commons.get_service_version.cache_clear()
    service_info.get_service_info.cache_clear()
    yield
    Commons.get_service_version.cache_clear()
    service_info.get_service_info.cache_clear()


# --- version ------------------------------------------------------------------------------------------------------------

def test_version_has_no_trailing_newline(fresh_version):
    version = Commons.get_service_version()
    assert version == _changelog_version()
    assert version == version.strip()


def test_version_script_runs_once(fresh_version, monkeypatch):
    calls = []
    monkeypatch.setattr(Commons, 'execute_script', classmethod(lambda cls, *args: calls.append(args) or '1.2.3\n'))
    assert Commons.get_service_version() == '1.2.3'
    assert Commons.get_service_version() == '1.2.3'
    assert len(calls) == 1


def test_service_info_is_built_once(fresh_version, monkeypatch):
    builds = []
    monkeypatch.setattr(service_info, 'load_service_info', lambda: builds.append(1) or 'info')
    assert service_info.get_service_info() == service_info.get_service_info() == 'info'
    assert builds == [1]


def test_version_setting_overrides_changelog(fresh_version):
    info = service_info.load_service_info({'service_info': {'version': ' 0.2.0+c3a9f23\n'}})
    assert info.version == '0.2.0+c3a9f23'


# --- defaults and settings --------------------------------------------------------------------------------------------

def test_defaults_are_neutral(fresh_version):
    info = service_info.load_service_info(get_properties())  # the repository's settings.toml
    answer = info.json(exclude_none=True)
    assert 'fi.muni.cz' not in answer and 'Masaryk' not in answer
    assert info.id == service_info.DEFAULT_ID
    assert info.name == service_info.DEFAULT_NAME
    assert info.organization.name == service_info.DEFAULT_ORGANIZATION_NAME
    assert info.organization.url == service_info.DEFAULT_ORGANIZATION_URL
    assert (info.type.group, info.type.artifact, info.type.version) == ('org.ga4gh', 'tes', '1.0.0')
    assert info.version == _changelog_version()
    for unset in ('contactUrl', 'documentationUrl', 'environment', 'createdAt', 'updatedAt', 'storage'):
        assert getattr(info, unset) is None, unset


def test_settings_describe_the_deployment():
    info = service_info.load_service_info({'service_info': {
        'id': 'org.example.tes',
        'name': 'Example TES',
        'description': 'TES for the example workspace',
        'organization': {'name': 'Example CO', 'url': 'https://example.org/'},
        'contact_url': 'mailto:support@example.org',
        'documentation_url': 'https://docs.example.org/tes',
        'environment': 'prod',
        'storage': ['s3://bucket/', 'https://s3.example.org'],
        'created_at': '2026-01-31T00:00:00Z',
        'updated_at': '2026-10-28T12:00:00Z',
    }}, version='9.9.9')
    assert info.id == 'org.example.tes'
    assert info.name == 'Example TES'
    assert info.description == 'TES for the example workspace'
    assert info.organization.name == 'Example CO'
    assert info.organization.url == 'https://example.org/'
    assert info.contactUrl == 'mailto:support@example.org'
    assert info.documentationUrl == 'https://docs.example.org/tes'
    assert info.environment == 'prod'
    assert info.storage == ['s3://bucket/', 'https://s3.example.org']
    assert info.createdAt.year == 2026 and info.updatedAt.month == 10


def test_settings_from_the_environment(monkeypatch):
    monkeypatch.setenv('TESP_API_SERVICE_INFO__ORGANIZATION__NAME', 'Workspace CO')
    monkeypatch.setenv('TESP_API_SERVICE_INFO__ENVIRONMENT', 'test')
    info = service_info.load_service_info(get_properties(), version='1.0.0')
    assert info.organization.name == 'Workspace CO'
    assert info.environment == 'test'


@pytest.mark.parametrize('settings, storage', [
    ({'s3': {'endpoint_url': 'https://s3.example.org'}}, ['https://s3.example.org']),
    ({'s3': {'endpoint_url': ''}}, None),
    ({'service_info': {'storage': 's3://one/'}, 's3': {'endpoint_url': 'https://s3.example.org'}}, ['s3://one/']),
    ({'service_info': {'storage': ['', ' ']}}, None),
])
def test_storage(settings, storage):
    assert service_info.load_service_info(settings, version='1').storage == storage


def test_invalid_url_is_refused():
    with pytest.raises(pydantic.ValidationError):
        service_info.load_service_info({'service_info': {'organization': {'url': 'not a url'}}}, version='1')


# --- endpoint -----------------------------------------------------------------------------------------------------------

@pytest.fixture
def client(fresh_version, monkeypatch):
    # The real task repository imports motor 2.5, which needs Python < 3.11 (asyncio.coroutine).
    repository = types.ModuleType('tesp_api.repository.task_repository')
    repository.task_repository = None
    monkeypatch.setitem(sys.modules, 'tesp_api.repository.task_repository', repository)
    for name in ('tesp_api.service.error', 'tesp_api.api.endpoints.endpoint_utils',
                 'tesp_api.api.endpoints.task_endpoints'):
        monkeypatch.setitem(sys.modules, name, None)  # restored after the test
        del sys.modules[name]
    task_endpoints = importlib.import_module('tesp_api.api.endpoints.task_endpoints')
    app = FastAPI()
    app.include_router(task_endpoints.router, prefix='/v1')
    return TestClient(app)


def test_endpoint_answers_without_nulls(client):
    answer = client.get('/v1/service-info')
    assert answer.status_code == 200
    body = answer.json()
    assert body['version'] == _changelog_version()
    assert body['organization'] == {'name': service_info.DEFAULT_ORGANIZATION_NAME,
                                    'url': service_info.DEFAULT_ORGANIZATION_URL}
    assert None not in body.values()
    assert 'contactUrl' not in body and 'environment' not in body
