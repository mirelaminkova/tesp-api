import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import List, Optional
from urllib.parse import urlsplit

import botocore.session
from aiobotocore.session import get_session
from botocore import UNSIGNED
from botocore.config import Config

from tesp_api.config.properties import properties
from tesp_api.service.client.client import Client
from tesp_api.repository.model.task import TesTaskIOType
from tesp_api.utils.types import S3Url

ADDRESSING_STYLES = {'path', 'virtual', 'auto'}
MAX_PRESIGN_EXPIRY_SECONDS = 7 * 24 * 3600  # SigV4 limit

# Query parameters of presigned URLs and form fields of presigned POSTs (lower case), e.g. `x-amz-signature=...`.
# The POST policy is redacted too: it contains the x-amz-credential condition, i.e. the access key id.
_PRESIGNED_SECRET_PARAMS = re.compile(
    r'((?<![\w-])(?i:X-Amz-(?:Credential|Signature|Security-Token)|policy)=)[^&\s\'"]+')


@dataclass(frozen=True)
class S3Config:
    """
    Server-side S3 settings (``s3.*`` in settings.toml, ``TESP_API_S3__*`` in the environment).
    Without an access key the server uses anonymous (unsigned) requests, which only work for public buckets.
    """
    endpoint_url: Optional[str] = None
    region: str = 'us-east-1'
    access_key_id: Optional[str] = None
    secret_access_key: Optional[str] = field(default=None, repr=False)
    addressing_style: str = 'path'
    presign_expiry_seconds: int = 3600
    output_presign_expiry_seconds: int = 24 * 3600

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
        if not 0 < self.output_presign_expiry_seconds <= MAX_PRESIGN_EXPIRY_SECONDS:
            raise ValueError(f's3.output_presign_expiry_seconds must be between 1 and {MAX_PRESIGN_EXPIRY_SECONDS}')

    @property
    def signed(self) -> bool:
        return self.access_key_id is not None


def load_s3_config(props=None) -> S3Config:
    s3 = (props if props is not None else properties).get('s3') or {}

    def setting(name):
        value = s3.get(name)
        # Dynaconf parses environment values, e.g. a secret key "123456" would become an int.
        return None if value in (None, '') else str(value)

    return S3Config(
        endpoint_url=setting('endpoint_url'),
        region=setting('region') or 'us-east-1',
        access_key_id=setting('access_key_id'),
        secret_access_key=setting('secret_access_key'),
        addressing_style=setting('addressing_style') or 'path',
        presign_expiry_seconds=int(setting('presign_expiry_seconds') or 3600),
        output_presign_expiry_seconds=int(setting('output_presign_expiry_seconds') or 24 * 3600))


@lru_cache(maxsize=1)
def get_s3_config() -> S3Config:
    """The server's S3 settings, read once."""
    return load_s3_config()


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
    config = config or get_s3_config()
    return _presign_client(config).generate_presigned_url(
        'get_object',
        Params={'Bucket': s3_url.bucket, 'Key': s3_url.key},
        ExpiresIn=config.presign_expiry_seconds)


def presign_s3_inputs(input_confs: List[dict], config: Optional[S3Config] = None) -> List[dict]:
    """
    Returns a copy of the stage-in ``input_confs`` in which S3 URLs are replaced by presigned URLs; those confs are
    marked with ``'presigned': True``.
    """
    staged = []
    for conf in input_confs:
        url = conf.get('url')
        if isinstance(url, S3Url):
            if conf.get('type') == TesTaskIOType.DIRECTORY:
                raise ValueError('S3 directory inputs are not supported')
            conf = {**conf, 'url': presign_get_url(url, config), 'presigned': True}
        staged.append(conf)
    return staged


def presign_put_url(s3_url: S3Url, config: Optional[S3Config] = None) -> str:
    """
    Returns an http(s) URL to which the object can be uploaded with ``PUT`` without credentials, valid for
    ``output_presign_expiry_seconds``. Outputs are uploaded after the executors finish, so this lifetime must cover the
    whole run of the task.
    """
    _require_plain(s3_url)
    config = config or get_s3_config()
    return _presign_client(config).generate_presigned_url(
        'put_object',
        Params={'Bucket': s3_url.bucket, 'Key': s3_url.key},
        ExpiresIn=config.output_presign_expiry_seconds)


def presign_post_policy(s3_url: S3Url, config: Optional[S3Config] = None) -> dict:
    """
    Returns a presigned POST (browser-based upload) that allows uploading any number of objects whose keys start with
    the key of ``s3_url`` followed by ``/``, valid for ``output_presign_expiry_seconds``. Used for DIRECTORY outputs,
    whose file names are only known after the task ran.

    Result: ``url`` to POST to, ``fields`` to send before the file (policy and signature), and ``key_prefix``; the
    ``key`` field, which must start with ``key_prefix``, is added per file.
    """
    _require_plain(s3_url)
    config = config or get_s3_config()
    key_prefix = s3_url.key.rstrip('/') + '/'
    # A key ending in ${filename} makes botocore add the condition ["starts-with", "$key", key_prefix].
    post = _presign_client(config).generate_presigned_post(
        s3_url.bucket, key_prefix + '${filename}', ExpiresIn=config.output_presign_expiry_seconds)
    fields = {name: value for name, value in post['fields'].items() if name != 'key'}
    return {'url': post['url'], 'fields': fields, 'key_prefix': key_prefix}


def presign_s3_outputs(output_confs: List[dict], config: Optional[S3Config] = None) -> List[dict]:
    """
    Returns a copy of the stage-out ``output_confs`` in which S3 outputs are replaced by presigned uploads, marked with
    ``'presigned': True``: a FILE gets a presigned ``PUT`` URL (``'upload': 's3_put'``), a DIRECTORY a presigned POST
    policy for its key prefix (``'upload': 's3_post'`` with ``form_fields`` and ``key_prefix``).
    """
    staged = []
    for conf in output_confs:
        url = conf.get('url')
        if isinstance(url, S3Url):
            if conf.get('type') == TesTaskIOType.DIRECTORY:
                post = presign_post_policy(url, config)
                conf = {**conf, 'url': post['url'], 'upload': 's3_post', 'form_fields': post['fields'],
                        'key_prefix': post['key_prefix'], 'presigned': True}
            else:
                conf = {**conf, 'url': presign_put_url(url, config), 'upload': 's3_put', 'presigned': True}
        staged.append(conf)
    return staged


def redact_presigned_urls(text: str) -> str:
    """
    Hides the access key id, signature and session token of presigned URLs and presigned POST fields in ``text``
    (for logging).
    """
    return _PRESIGNED_SECRET_PARAMS.sub(r'\1REDACTED', text)


# TODO: Error handling, maybe_of
class ClientS3(Client):

    @staticmethod
    async def download_file(s3_url: S3Url):
        _require_plain(s3_url)
        async with get_session().create_client('s3', **_client_args(get_s3_config())) as client:
            async with (await client.get_object(Bucket=s3_url.bucket, Key=s3_url.key))['Body'] as stream:
                return await stream.read()

    @staticmethod
    async def upload_file(s3_url: S3Url, file_content: bytes):
        _require_plain(s3_url)
        async with get_session().create_client('s3', **_client_args(get_s3_config())) as client:
            await client.put_object(Bucket=s3_url.bucket, Key=s3_url.key, Body=file_content)
