from dataclasses import dataclass
from functools import lru_cache
from string import Formatter
from typing import Optional

from tesp_api.config.properties import properties
from tesp_api.utils.resource_values import positive, ram_mib

RESOURCE_LIMIT_MODES = ('auto', 'always', 'never')
# Runtimes whose limit flags work without extra host setup; "auto" applies limits only for these.
_AUTO_LIMIT_RUNTIMES = {'docker'}


def _cpu_values(cpu_cores) -> dict:
    return {'cpu_cores': cpu_cores}


def _ram_values(ram_gb) -> dict:
    return {'ram_gb': ram_gb, 'ram_mib': ram_mib(ram_gb)}


def _check_template(name: str, template: str, values: dict):
    """Each template may only use the placeholders of its own resource, which are set whenever it is used."""
    try:
        fields = {field for _, field, _, _ in Formatter().parse(template) if field is not None}
    except ValueError as e:
        raise ValueError(f'{name}: {e}') from None
    if not fields <= values.keys():
        raise ValueError(f'{name} may only use the placeholders {sorted("{" + key + "}" for key in values)}')
    try:
        template.format(**values)  # e.g. a format spec that does not fit
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
    ``--cpus-per-task={cpu_cores}`` and ``--mem={ram_mib}`` for SLURM), each used only when the task requests that
    resource. Placeholders: ``{cpu_cores}`` for the CPU template, ``{ram_gb}`` and ``{ram_mib}`` (ram_gb in whole MiB,
    rounded up) for the memory template.
    """
    container_limits: str = 'auto'
    native_spec_cpu_cores: str = ''
    native_spec_ram_gb: str = ''

    def __post_init__(self):
        if self.container_limits not in RESOURCE_LIMIT_MODES:
            raise ValueError(f'container.resource_limits must be one of {", ".join(RESOURCE_LIMIT_MODES)}, '
                             f'got {self.container_limits!r}')
        _check_template('pulsar.native_specification.cpu_cores', self.native_spec_cpu_cores, _cpu_values(2))
        _check_template('pulsar.native_specification.ram_gb', self.native_spec_ram_gb, _ram_values(1.5))

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
        parts = []
        if self.native_spec_cpu_cores and cpu_cores is not None:
            parts.append(self.native_spec_cpu_cores.format(**_cpu_values(cpu_cores)))
        if self.native_spec_ram_gb and ram_gb is not None:
            parts.append(self.native_spec_ram_gb.format(**_ram_values(ram_gb)))
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
