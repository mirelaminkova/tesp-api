"""
``GET /v1/service-info``, built once from the ``service_info.*`` settings (issue #69).

Every deployment used to claim to be Masaryk University's ``dev`` service, and the version was read by running
``ci/version.sh`` on each request, with its trailing newline.
"""
from functools import lru_cache
from typing import List, Optional

from tesp_api.api.model.task_service_info import TesServiceInfo, TesServiceOrganization, TesServiceType
from tesp_api.config.properties import properties
from tesp_api.utils.commons import Commons

# The TES specification version this server implements.
TES_VERSION = '1.0.0'

DEFAULT_ID = 'tesp-api'
DEFAULT_NAME = 'TESP API'
DEFAULT_DESCRIPTION = 'GA4GH TES server implementation for Pulsar'
# The software's home: describes what runs, not who runs it. Deployments set service_info.organization.*.
DEFAULT_ORGANIZATION_NAME = 'TESP API'
DEFAULT_ORGANIZATION_URL = 'https://github.com/CESNET/tesp-api'


def _setting(settings: dict, name: str) -> Optional[str]:
    value = settings.get(name)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _storage(settings: dict, props) -> Optional[List[str]]:
    storage = settings.get('storage')
    if isinstance(storage, str):
        storage = [storage]
    storage = [str(location).strip() for location in storage or [] if str(location).strip()]
    if not storage:
        # S3 inputs and outputs (s3.*) are the storage tesp-api itself can use.
        endpoint = _setting(props.get('s3') or {}, 'endpoint_url')
        storage = [endpoint] if endpoint else []
    return storage or None


def load_service_info(props=None, version: Optional[str] = None) -> TesServiceInfo:
    """The service info from ``service_info.*``; unset fields get neutral defaults or are left out.

    ``service_info.version`` (e.g. set from a build with the commit) overrides the version of the latest release in
    CHANGELOG.md, ``version`` (for tests) both."""
    props = props if props is not None else properties
    settings = props.get('service_info') or {}
    organization = settings.get('organization') or {}
    return TesServiceInfo(
        id=_setting(settings, 'id') or DEFAULT_ID,
        name=_setting(settings, 'name') or DEFAULT_NAME,
        type=TesServiceType(group='org.ga4gh', artifact='tes', version=TES_VERSION),
        description=_setting(settings, 'description') or DEFAULT_DESCRIPTION,
        organization=TesServiceOrganization(
            name=_setting(organization, 'name') or DEFAULT_ORGANIZATION_NAME,
            url=_setting(organization, 'url') or DEFAULT_ORGANIZATION_URL,
        ),
        contactUrl=_setting(settings, 'contact_url'),
        documentationUrl=_setting(settings, 'documentation_url'),
        createdAt=_setting(settings, 'created_at'),
        updatedAt=_setting(settings, 'updated_at'),
        environment=_setting(settings, 'environment'),
        version=version or _setting(settings, 'version') or Commons.get_service_version(),
        storage=_storage(settings, props),
    )


@lru_cache(maxsize=1)
def get_service_info() -> TesServiceInfo:
    """The server's service info, built once."""
    return load_service_info()
