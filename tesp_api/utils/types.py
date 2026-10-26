from pydantic.networks import AnyUrl
import pydantic.errors as errors


class FtpUrl(AnyUrl):
    allowed_schemes = {'ftp'}
    host_required = True

class HttpUrl(AnyUrl):
    allowed_schemes = {'http'}
    host_required = True

class HttpsUrl(AnyUrl):
    allowed_schemes = {'https'}
    host_required = True

class S3Url(AnyUrl):
    """``s3://bucket/key``. The S3 endpoint and credentials come from the server's ``s3`` settings."""
    allowed_schemes = {'s3'}
    host_required = True

    @property
    def bucket(self) -> str:
        return self.host

    @property
    def key(self) -> str:
        # The key is taken literally (no percent-decoding), like the AWS CLI does.
        return self.path[1:] if self.path else ''

    def is_plain(self) -> bool:
        """False for legacy ``s3://user:pass@host:port/bucket/key`` URLs and anything that is not bucket + key."""
        return not (self.user or self.password or self.port or self.query or self.fragment) and bool(self.key)

class LocalFileUrl(AnyUrl):
    # No scheme needed for local file paths, but we don't need to enforce any particular scheme
    host_required = False  # Local files don't have a host
    allowed_schemes = {''}  # No scheme needed for file paths

    # Custom validation to check if it's a valid path
    @classmethod
    def __get_validators__(cls):
        yield from super().__get_validators__()
        yield cls.validate_local_path

    @classmethod
    def validate_local_path(cls, v):
        # Ensure the path is a valid file path (basic check for now, you can extend this)
        if not isinstance(v, str):
            raise errors.UrlError("Invalid local file path")
        # Optional: Add further validation if necessary (e.g., check if the file exists)
        return v
