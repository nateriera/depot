#!/usr/bin/env python3
"""Install Linux systemd user services. --prefix and --no-start support bounded staging."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
sys.dont_write_bytecode = True
from configuration import DEFAULT_PORT, port_number


def systemd_quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def desktop_quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"'


def install(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = Path(__file__).resolve().parent.parent
    parser.add_argument('--prefix', type=Path, help='stage under DIR/config, DIR/share; does not change the active user installation')
    parser.add_argument('--no-start', action='store_true', help='write files without calling systemctl')
    parser.add_argument('--yard', type=Path, default=Path(os.environ.get('DEPOT_YARD', source / 'yard')))
    parser.add_argument('--port', type=port_number, default=os.environ.get('DEPOT_FLOOR_PORT', str(DEFAULT_PORT)))
    parser.add_argument('--vault', type=Path, help='explicit existing note directory; default: integration disabled')
    parser.add_argument('--path', default=os.environ.get('PATH', os.defpath), help='PATH available to services and adapter discovery')
    parser.add_argument('--codex', default=os.environ.get('DEPOT_CODEX', ''))
    parser.add_argument('--claude', default=os.environ.get('DEPOT_CLAUDE', ''))
    parser.add_argument('--adapters', choices=('codex', 'claude', 'codex,claude', 'none'), default='codex')
    args = parser.parse_args(argv)
    if args.prefix and not args.no_start:
        parser.error('--prefix is staging only; also pass --no-start')
    yard = args.yard.expanduser().resolve()
    vault = str(args.vault.expanduser().resolve()) if args.vault else ''
    if vault and not Path(vault).is_dir():
        parser.error('--vault must name an existing directory')
    if args.prefix:
        prefix = args.prefix.expanduser().resolve()
        config_home, data_home = prefix / 'config', prefix / 'share'
    else:
        config_home = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
        data_home = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))
    config = config_home.resolve() / 'depot/config.env'
    selected = '' if args.adapters == 'none' else args.adapters.replace(',', ' ')
    values = {'DEPOT_YARD': str(yard), 'DEPOT_FLOOR_PORT': str(args.port), 'DEPOT_VAULT': vault,
              'DEPOT_CODEX': args.codex, 'DEPOT_CLAUDE': args.claude, 'DEPOT_ADAPTERS': selected,
              'PATH': args.path, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1'}
    if any(any(c in v for c in '\r\n\0') for v in [*values.values(), str(source), str(config), sys.executable]):
        parser.error('configuration values cannot contain control characters')
    if not args.no_start:
        if not shutil.which('systemctl'):
            parser.error('systemctl is required; use --prefix DIR --no-start for staging')
        for adapter in selected.split():
            binary = values['DEPOT_' + adapter.upper()] or adapter
            if not shutil.which(binary, path=args.path):
                parser.error(f'{adapter} is not available under the configured PATH or adapter override')
    # Validate everything before creating files. Existing operational records are never overwritten.
    (yard / 'items').mkdir(parents=True, exist_ok=True)
    (yard / 'log').mkdir(exist_ok=True)
    for file, content in [('log/events.jsonl', ''), ('log/vault-captures.json', '{}\n'),
                          ('tracks.json', '{"_default": {"timeout_s": 900, "quiet_after_s": 300}}\n')]:
        path = yard / file
        if not path.exists():
            path.write_text(content)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(''.join(f'{key}={json.dumps(value, ensure_ascii=False)}\n' for key, value in values.items()))
    config.chmod(0o600)
    units = config_home / 'systemd/user'
    units.mkdir(parents=True, exist_ok=True)
    replacements = {'@CONFIG@': systemd_quote(config), '@YARD@': systemd_quote(yard),
                    '@PYTHON@': systemd_quote(sys.executable).replace('$', '$$'),
                    '@SCRIPT@': systemd_quote(source / 'turntable/depot.py').replace('$', '$$')}
    for unit in ('depot-poll.service', 'depot-floor.service'):
        text = (source / 'ops' / unit).read_text()
        for key, value in replacements.items():
            text = text.replace(key, value)
        (units / unit).write_text(text)
    apps = data_home / 'applications'
    apps.mkdir(parents=True, exist_ok=True)
    launcher = (source / 'ops/depot-floor.desktop').read_text()
    for key, value in {'@PYTHON@': desktop_quote(sys.executable), '@LAUNCHER@': desktop_quote(source / 'ops/launch.py'),
                       '@CONFIG@': desktop_quote(config)}.items():
        launcher = launcher.replace(key, value)
    (apps / 'depot-floor.desktop').write_text(launcher)
    if not args.no_start:
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', '--user', 'enable', '--now', 'depot-poll.service', 'depot-floor.service'], check=True)
        subprocess.run(['systemctl', '--user', 'restart', 'depot-poll.service', 'depot-floor.service'], check=True)
    print(f'Configuration: {config}\nUnits: {units}\nYard: {yard}\nFloor: http://127.0.0.1:{args.port}/')
    print('Staged only; no services changed.' if args.no_start else 'Linux user services enabled and restarted.')
    return config


if __name__ == '__main__':
    install()
