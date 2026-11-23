#!/usr/bin/env python3
"""Check a configured Linux/systemd user installation without changing it."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
sys.dont_write_bytecode = True
from configuration import read_config, port_number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'depot/config.env')
    args = parser.parse_args(argv)
    settings = read_config(args.config)
    for unit in ('depot-poll.service', 'depot-floor.service'):
        for state in ('is-active', 'is-enabled'):
            subprocess.run(['systemctl', '--user', state, '--quiet', unit], check=True)
    port = port_number(os.environ.get('DEPOT_FLOOR_PORT', settings['DEPOT_FLOOR_PORT']))
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=5) as response:
        if response.status != 200:
            raise SystemExit('floor did not return HTTP 200')
    env = {**os.environ, **settings}
    for adapter in settings['DEPOT_ADAPTERS'].split():
        binary = settings.get('DEPOT_' + adapter.upper()) or adapter
        found = shutil.which(binary, path=settings['PATH'])
        if not found:
            raise SystemExit(f'{adapter} does not resolve under the configured PATH or override')
        subprocess.run([found, '--version'], env=env, check=True, capture_output=True)
    pid = int((Path(settings['DEPOT_YARD']) / 'log/poll.lock').read_text())
    os.kill(pid, 0)
    service_pid = int(subprocess.check_output(['systemctl', '--user', 'show', 'depot-poll.service', '-p', 'MainPID', '--value'], text=True).strip())
    if pid != service_pid:
        raise SystemExit('poll lock does not belong to the configured poll service')
    print(f'Depot is standing on 127.0.0.1:{port}; selected adapters resolve and the service owns its yard lock.')


if __name__ == '__main__':
    main()
