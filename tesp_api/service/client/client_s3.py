import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional
from urllib.parse import urlsplit

import botocore.session
from aiobotocore.session import get_session
from botocore import UNSIGNED
from botocore.config import Config

from tesp_api.config.properties import properties
from tesp_api.service.client.client import Client
from tesp_api.utils.types import S3Url

ADDRESSING_STYLES = {'path', 'virtual', 'auto'}
MAX_PRESIGN_EXPIRY_SECONDS = 7 * 24 * 3600  # SigV4 limit

_PRESIGNED_SECRET_PARAMS = re.compile(r'(X-Amz-(?:Credential|Signature|Security-Token)=)[^&\s\'"]+')


@dataclass(frozen=True)
class S3Config:
    """
    Server-side S3 settings (``s3.*`` in settings.toml, ``TESP_API_S3__*`` in the environment).
    Without an access key the server uses anonymous (unsigned) requests, which only work for public buckets.
    """
    endpoint_url: Optional[str] = None
    region: Optional[str] = None
    access_key_id: Optional[str] = None
    secret_access_key: Optional[str] = field(default=None, repr=False)
    addressing_style: str = 'path'
    presign_expiry_seconds: int = 3600

    def __post_init__(self):
        # Messages must never include the secret key.
        if self.endpoint_url is not None:
            parts = urlsplit(self.endpoint_url)
            if parts.scheme not in ('http', 'https') or not parts.netloc:
                raise ValueError('s3.endpoint_url must be an http:// or https:// URL')
        if self.addressing_style not in ADDRESSING_STYLES:
            raise ValueError(f's3.addressing_style must be one of {sorted(ADDRESSING_STYLES)}')
        if (self.access_key_id is None) != (self.secret_access_key is None):
            raise ValueError('s3.access_key_id and s3.secret_access_key must be set together')
        if not 0 < self.presign_expiry_seconds <= MAX_PRESIGN_EXPIRY_SECONDS:
            raise ValueError(f's3.presign_expiry_seconds must be between 1 and {MAX_PRESIGN_EXPIRY_SECONDS}')

    @property
    def signed(self) -> bool:
        return self.access_key_id is not None


def load_s3_config(props=None) -> S3Config:
    s3 = (props if props is not None else properties).get('s3') or {}

    def setting(name):
        value = s3.get(name)
        return None if value in (None, '') else value

    return S3Config(
        endpoint_url=setting('endpoint_url'),
        region=setting('region'),
        access_key_id=setting('access_key_id'),
        secret_access_key=setting('secret_access_key'),
        addressing_style=setting('addressing_style') or 'path',
        presign_expiry_seconds=int(setting('presign_expiry_seconds') or 3600))


def _client_args(config: S3Config) -> dict:
    return dict(
        region_name=config.region,
        endpoint_url=config.endpoint_url,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        config=Config(
            signature_version='s3v4' if config.signed else UNSIGNED,
            s3={'addressing_style': config.addressing_style}))


@lru_cache(maxsize=4)
def _presign_client(config: S3Config):
    return botocore.session.get_session().create_client('s3', **_client_args(config))


def _require_plain(s3_url: S3Url):
    if not s3_url.is_plain():
        raise ValueError('S3 URLs must have the form s3://bucket/key; endpoint and credentials are '
                         'configured on the server')


def presign_get_url(s3_url: S3Url, config: Optional[S3Config] = None) -> str:
    """
    Returns an http(s) URL from which the object can be downloaded without credentials: a presigned URL valid for
    ``presign_expiry_seconds``, or a plain URL when the server has no S3 credentials. Only the signature leaves the
    server, never the secret key. Presigning is local, no request is made to S3.
    """
    _require_plain(s3_url)
    config = config or load_s3_config()
    return _presign_client(config).generate_presigned_url(
        'get_object',
        Params={'Bucket': s3_url.bucket, 'Key': s3_url.key},
        ExpiresIn=config.presign_expiry_seconds)


def redact_presigned_urls(text: str) -> str:
    """Hides the access key id and signature of presigned URLs in ``text`` (for logging)."""
    return _PRESIGNED_SECRET_PARAMS.sub(r'\1REDACTED', text)


# TODO: Error handling, maybe_of
class ClientS3(Client):

    @staticmethod
    async def download_file(s3_url: S3Url):
        _require_plain(s3_url)
        async with get_session().create_client('s3', **_client_args(load_s3_config())) as client:
            async with (await client.get_object(Bucket=s3_url.bucket, Key=s3_url.key))['Body'] as stream:
                return await stream.read()

    @staticmethod
    async def upload_file(s3_url: S3Url, file_content: bytes):
        _require_plain(s3_url)
        async with get_session().create_client('s3', **_client_args(load_s3_config())) as client:
            await client.put_object(Bucket=s3_url.bucket, Key=s3_url.key, Body=file_content)
