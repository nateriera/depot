"""Public installation and fictional-fixture contracts; all writes use disposable directories."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

OPS = Path(__file__).resolve().parent.parent / 'ops'
sys.path.insert(0, str(OPS))
import install as setup
import launch
import check as checker
from configuration import read_config
import demo_yard
import floor


class TestPublicSetup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def stage(self, *more):
        with contextlib.redirect_stdout(io.StringIO()):
            return setup.install(['--prefix', str(self.root / 'installation with spaces'), '--no-start',
                                  '--yard', str(self.root / 'records with spaces'), '--port', '18877',
                                  '--adapters', 'none', *more])

    def test_staging_is_bounded_has_empty_yard_explicit_port_and_no_service_calls(self):
        with mock.patch.object(subprocess, 'run', side_effect=AssertionError('must not contact services')):
            config = self.stage()
        settings = read_config(config)
        self.assertEqual(settings['DEPOT_FLOOR_PORT'], '18877')
        self.assertEqual(settings['DEPOT_VAULT'], '')
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        yard = Path(settings['DEPOT_YARD'])
        self.assertEqual(list((yard / 'items').iterdir()), [])
        self.assertEqual((yard / 'log/events.jsonl').read_text(), '')
        self.assertEqual(set(json.loads((yard / 'tracks.json').read_text())), {'_default'})
        units = config.parent.parent / 'systemd/user'
        for name in ['depot-floor.service', 'depot-poll.service']:
            unit = (units / name).read_text()
            self.assertIn('EnvironmentFile="' + str(config) + '"', unit)
            self.assertNotIn('@', unit)
            self.assertIn('WorkingDirectory="' + str(yard) + '"', unit)
        desktop = config.parent.parent.parent / 'share/applications/depot-floor.desktop'
        self.assertIn('--config', desktop.read_text())
        self.assertNotIn('8765', desktop.read_text())

    def test_vault_requires_explicit_existing_directory_and_reinstall_can_disable(self):
        vault = self.root / 'chosen notes'
        vault.mkdir()
        config = self.stage('--vault', str(vault))
        self.assertEqual(read_config(config)['DEPOT_VAULT'], str(vault))
        config = self.stage()
        self.assertEqual(read_config(config)['DEPOT_VAULT'], '')

    def test_invalid_configuration_writes_nothing(self):
        for flags in [('--port', '0'), ('--port', '65536'), ('--vault', str(self.root / 'missing'))]:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.stage(*flags)
            self.assertEqual(list(self.root.iterdir()), [])

    def test_reinstall_preserves_existing_records(self):
        config = self.stage()
        log = Path(read_config(config)['DEPOT_YARD']) / 'log/events.jsonl'
        log.write_text('existing record\n')
        self.stage()
        self.assertEqual(log.read_text(), 'existing record\n')

    def test_adapter_overrides_and_nonascii_paths_roundtrip_as_literal_data(self):
        binary = str(self.root / 'tools café' / 'adapter')
        config = self.stage('--codex', binary, '--path', str(self.root / 'tools café'), '--adapters', 'codex')
        settings = read_config(config)
        self.assertEqual(settings['DEPOT_CODEX'], binary)
        self.assertEqual(settings['DEPOT_ADAPTERS'], 'codex')
        self.assertEqual(settings['PATH'], str(self.root / 'tools café'))

    def test_launcher_reads_selected_port_and_allows_an_explicit_override(self):
        config = self.stage()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(subprocess, 'run') as run:
            launch.main(['--config', str(config)])
            self.assertEqual(run.call_args.args[0], ['xdg-open', 'http://127.0.0.1:18877/'])
        with mock.patch.dict(os.environ, {'DEPOT_FLOOR_PORT': '18878'}), mock.patch.object(subprocess, 'run') as run:
            launch.main(['--config', str(config)])
            self.assertEqual(run.call_args.args[0], ['xdg-open', 'http://127.0.0.1:18878/'])

    def test_checker_uses_selected_adapter_override_port_and_own_service_lock(self):
        config = self.stage('--adapters', 'claude', '--claude', 'selected-adapter')
        settings = read_config(config)
        (Path(settings['DEPOT_YARD']) / 'log/poll.lock').write_text('1234')
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(checker.urllib.request, 'urlopen', return_value=response) as http, \
                mock.patch.object(subprocess, 'run') as run, mock.patch.object(subprocess, 'check_output', return_value='1234\n'), \
                mock.patch.object(checker.shutil, 'which', return_value='/fake/selected-adapter') as which, \
                mock.patch.object(os, 'kill'), contextlib.redirect_stdout(io.StringIO()):
            checker.main(['--config', str(config)])
        self.assertEqual(http.call_args.args[0], 'http://127.0.0.1:18877/')
        self.assertEqual(which.call_args.args[0], 'selected-adapter')
        self.assertIn(['/fake/selected-adapter', '--version'], [call.args[0] for call in run.call_args_list])

    def test_demo_generation_is_reproducible_and_uses_synthetic_evidence(self):
        one = demo_yard.build(self.root / 'one')
        two = demo_yard.build(self.root / 'two')
        read = lambda root: {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        self.assertEqual(read(one), read(two))
        events = floor.read_events(one / 'log/events.jsonl')
        self.assertTrue(all(e['ts'].startswith('2040-') for e in events))
        self.assertTrue(all('session_id' not in e and 'thread_id' not in e for e in events))
        self.assertEqual(len(floor.project(one, demo_yard.DEMO_NOW)['cards']), 12)
        self.assertIn('tests_failed', {e['data'].get('result') for e in events})


if __name__ == '__main__':
    unittest.main()
