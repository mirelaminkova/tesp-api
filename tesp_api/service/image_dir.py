import os
import posixpath
from functools import lru_cache
from typing import Optional

from loguru import logger

from tesp_api.config.properties import properties


def load_allowed_image_dir(props=None, container_type: Optional[str] = None) -> Optional[str]:
    """
    ``container.allowed_image_dir``: the directory on the Pulsar host whose pre-pulled Singularity/Apptainer images
    executors may name by absolute path (issue #46), or ``None`` (the default) when host-path images are refused.
    It must be an absolute, normalized path other than ``/``. Docker has no equivalent, so the setting is ignored, with
    a warning, unless ``CONTAINER_TYPE`` is ``singularity``.
    """
    props = props if props is not None else properties
    container_type = container_type if container_type is not None else os.getenv("CONTAINER_TYPE", "docker")
    value = (props.get('container') or {}).get('allowed_image_dir')
    value = '' if value is None else str(value).strip()
    if not value:
        return None
    if not value.startswith('/') or posixpath.normpath(value) != value.rstrip('/') or value.rstrip('/') == '':
        raise ValueError(f'container.allowed_image_dir must be an absolute, normalized directory path other than "/", '
                         f'got {value!r}')
    if container_type != 'singularity':
        logger.warning(f'container.allowed_image_dir is set but CONTAINER_TYPE is {container_type!r}: it only applies '
                       f'to singularity and is ignored.')
        return None
    return value.rstrip('/')


@lru_cache(maxsize=1)
def get_allowed_image_dir() -> Optional[str]:
    """The server's ``container.allowed_image_dir``, read once."""
    return load_allowed_image_dir()
