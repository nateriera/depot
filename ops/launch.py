#!/usr/bin/env python3
"""Open the loopback floor using the same port configuration as the services."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
sys.dont_write_bytecode = True
from configuration import read_config, port_number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args(argv)
    settings = read_config(args.config)
    port = port_number(os.environ.get('DEPOT_FLOOR_PORT', settings['DEPOT_FLOOR_PORT']))
    subprocess.run(['xdg-open', f'http://127.0.0.1:{port}/'], check=True)


if __name__ == '__main__':
    main()
