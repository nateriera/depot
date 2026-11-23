"""Shared installation configuration. Files contain literal values, never executable shell code."""
import shlex
from pathlib import Path

DEFAULT_PORT = 8765


def read_config(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, value = line.split('=', 1)
        parts = shlex.split(value, comments=False)
        result[key] = parts[0] if parts else ''
    return result


def port_number(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError('port must be between 1 and 65535')
    return port
