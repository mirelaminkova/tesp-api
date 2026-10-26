import math
from typing import Optional

# Larger values (up to inf, which the API accepts for ram_gb) are no real request and are ignored like 0.
_MAX_RESOURCE = 2 ** 53


def positive(value) -> Optional[float]:
    """``value`` if it is a usable TES resource amount (a positive number), else ``None``: resources are hints."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= _MAX_RESOURCE:
        return value
    return None


def ram_mib(ram_gb: float) -> int:
    """TES ``ram_gb`` in whole MiB, rounded up so the limit is never below the request."""
    return math.ceil(ram_gb * 1024)
