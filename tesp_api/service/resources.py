import math
from dataclasses import dataclass
from functools import lru_cache
from string import Formatter
from typing import Optional

from tesp_api.config.properties import properties

RESOURCE_LIMIT_MODES = {'auto', 'always', 'never'}
# Runtimes whose limit flags work without extra host setup; "auto" applies limits only for these.
_AUTO_LIMIT_RUNTIMES = {'docker'}
_TEMPLATE_FIELDS = {'cpu_cores', 'ram_gb', 'ram_mb'}


def positive(value) -> Optional[float]:
    """``value`` if it is a positive number, else ``None``: TES resources are optional hints."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def ram_mib(ram_gb: float) -> int:
    """TES ``ram_gb`` in whole MiB, rounded up so the limit is never below the request."""
    return math.ceil(ram_gb * 1024)


def _check_template(name: str, template: str):
    try:
        fields = {field for _, field, _, _ in Formatter().parse(template) if field is not None}
    except ValueError as e:
        raise ValueError(f'{name}: {e}') from None
    if not fields <= _TEMPLATE_FIELDS:
        raise ValueError(f'{name} may only use the placeholders {sorted(_TEMPLATE_FIELDS)}')
    try:
        template.format(cpu_cores=1, ram_gb=1.5, ram_mb=1536)  # e.g. a format spec that does not fit
    except (ValueError, TypeError) as e:
        raise ValueError(f'{name}: {e}') from None


@dataclass(frozen=True)
class ResourceConfig:
    """
    How TES ``resources.cpu_cores`` and ``resources.ram_gb`` are enforced (``container.resource_limits`` and
    ``pulsar.native_specification.*`` in settings.toml).

    ``container_limits`` controls ``--cpus``/``--memory`` on the container command line: ``auto`` for docker only,
    ``always`` for every runtime, ``never`` for none. Apptainer needs a working cgroups manager for these flags and
    fails the task without one, hence ``auto`` leaves them out for singularity.

    ``native_spec_*`` are templates for the DRMAA native specification Pulsar sends to the batch system (e.g.
    ``--cpus-per-task={cpu_cores}`` and ``--mem={ram_mb}`` for SLURM), each used only when the task requests that
    resource. Placeholders: ``{cpu_cores}``, ``{ram_gb}`` and ``{ram_mb}`` (ram_gb in whole MiB, rounded up).
    """
    container_limits: str = 'auto'
    native_spec_cpu_cores: str = ''
    native_spec_ram_gb: str = ''

    def __post_init__(self):
        if self.container_limits not in RESOURCE_LIMIT_MODES:
            raise ValueError(f'container.resource_limits must be one of {sorted(RESOURCE_LIMIT_MODES)}')
        _check_template('pulsar.native_specification.cpu_cores', self.native_spec_cpu_cores)
        _check_template('pulsar.native_specification.ram_gb', self.native_spec_ram_gb)

    def container_resource_conf(self, resource_conf: Optional[dict], container_type: str) -> dict:
        """``resource_conf`` if limit flags go on the ``container_type`` command line, else ``{}``."""
        if self.container_limits == 'always' or \
                (self.container_limits == 'auto' and container_type in _AUTO_LIMIT_RUNTIMES):
            return resource_conf or {}
        return {}

    def native_specification(self, resource_conf: Optional[dict]) -> str:
        resource_conf = resource_conf or {}
        cpu_cores = positive(resource_conf.get('cpu_cores'))
        ram_gb = positive(resource_conf.get('ram_gb'))
        values = {'cpu_cores': cpu_cores, 'ram_gb': ram_gb, 'ram_mb': ram_gb and ram_mib(ram_gb)}
        parts = [template.format(**values) for template, requested in
                 ((self.native_spec_cpu_cores, cpu_cores), (self.native_spec_ram_gb, ram_gb))
                 if template and requested is not None]
        return ' '.join(parts)

    def submit_params(self, resource_conf: Optional[dict]) -> dict:
        """Pulsar submit parameters for the job; its DRMAA managers pass ``native_specification`` to the DRM."""
        native_specification = self.native_specification(resource_conf)
        return {'native_specification': native_specification} if native_specification else {}


def load_resource_config(props=None) -> ResourceConfig:
    props = props if props is not None else properties
    container = props.get('container') or {}
    native_spec = (props.get('pulsar') or {}).get('native_specification') or {}

    def setting(conf, name):
        value = conf.get(name)
        return '' if value is None else str(value).strip()

    return ResourceConfig(
        container_limits=setting(container, 'resource_limits').lower() or 'auto',
        native_spec_cpu_cores=setting(native_spec, 'cpu_cores'),
        native_spec_ram_gb=setting(native_spec, 'ram_gb'))


@lru_cache(maxsize=1)
def get_resource_config() -> ResourceConfig:
    """The server's resource settings, read once."""
    return load_resource_config()
