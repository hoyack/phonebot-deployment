"""No live Hermes, model calls, sockets or systemd changes: isolated HOME only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/setup-atlas-worker.py"


class AtlasWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.profile = self.home / ".hermes/profiles/atlas-work"
        self.units = self.home / "units"
        self.bridge = self.home / "bridge/bridge.env"
        for directory in (self.profile, self.units, self.bridge.parent):
            directory.mkdir(mode=0o700, parents=True)
        for directory in (self.home / '.hermes', self.home / '.hermes/profiles'):
            directory.chmod(0o700)
        self.config = self.profile / "config.yaml"
        self.private(self.config, yaml.safe_dump({
            "model": {"provider": "nous", "default": "test-model", "base_url": "https://example.invalid/v1"},
            "display": {"skin": "keep-me"},
        }))
        self.private(self.profile / ".env", "# keep\nNOUS_API_KEY=test-model-secret\nFIRECRAWL_API_KEY=test-web-secret\n")
        self.private(self.bridge, "# existing bridge\nHERMES_ATLAS_API_KEY=foreground-do-not-touch\n")
        self.executable = self.home / "fake hermes"
        self.executable.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys, yaml
assert sys.argv[1:3] == ['-p', 'atlas-work']
assert sys.argv[3:5] == ['config', 'set']
p = pathlib.Path(os.environ['HERMES_HOME']) / 'config.yaml'
assert p.parent == pathlib.Path(os.environ['HOME']) / '.hermes/profiles/atlas-work'
c = yaml.safe_load(p.read_text())
keys = sys.argv[5].split('.')
d = c
for key in keys[:-1]:
    d = d.setdefault(key, {{}})
d[keys[-1]] = yaml.safe_load(sys.argv[6])
p.write_text(yaml.safe_dump(c))
with (pathlib.Path(os.environ['HOME']) / 'calls.jsonl').open('a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
''')
        self.executable.chmod(0o700)

    def private(self, path, text):
        path.write_text(text)
        path.chmod(0o600)

    def run_setup(self, *extra):
        return subprocess.run([
            sys.executable, str(SCRIPT), '--profile-dir', str(self.profile),
            '--hermes-executable', str(self.executable), '--unit-dir', str(self.units),
            '--bridge-env', str(self.bridge), *map(str, extra),
        ], env={'HOME': str(self.home), 'PATH': os.environ['PATH']},
            capture_output=True, text=True)

    def env_values(self, path):
        return dict(line.split('=', 1) for line in path.read_text().splitlines()
                    if line and not line.startswith('#'))

    def test_restricts_only_explicit_worker_and_distributes_private_key(self):
        original_model = yaml.safe_load(self.config.read_text())['model']
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        c = yaml.safe_load(self.config.read_text())
        self.assertEqual(c['model'], original_model)
        self.assertEqual(c['display']['skin'], 'keep-me')
        self.assertEqual(c['platform_toolsets']['api_server'], ['web', 'no_mcp'])
        self.assertEqual(c['agent']['max_turns'], 8)
        self.assertFalse(c['memory']['memory_enabled'])
        self.assertFalse(c['memory']['user_profile_enabled'])
        self.assertEqual(c['approvals']['mode'], 'manual')
        for mode in ('cron_mode', 'single_query_mode', 'unattended_mode'):
            self.assertEqual(c['approvals'][mode], 'deny')
        self.assertFalse(c['gateway']['multiplex_profiles'])
        worker = self.env_values(self.profile / '.env')
        self.assertEqual(worker['NOUS_API_KEY'], 'test-model-secret')
        key = worker['API_SERVER_KEY']
        self.assertGreaterEqual(len(key), 43)
        bridge = self.env_values(self.bridge)
        self.assertEqual(bridge['HERMES_ATLAS_WORK_API_KEY'], key)
        self.assertEqual(bridge['HERMES_ATLAS_API_KEY'], 'foreground-do-not-touch')
        unit = self.units / 'hermes-atlas-work.service'
        content = unit.read_text()
        self.assertIn('gateway run --external-supervisor', content)
        self.assertNotIn('--replace', content)
        self.assertNotIn(key, content + result.stdout + result.stderr)
        for k, v in {'API_SERVER_ENABLED': 'true', 'API_SERVER_HOST': '127.0.0.1',
                     'API_SERVER_PORT': '8645', 'HERMES_SAFE_MODE': '1'}.items():
            self.assertEqual(worker[k], v)
        for path in (unit, self.config, self.profile / '.env', self.bridge):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        calls = [json.loads(line) for line in (self.home / 'calls.jsonl').read_text().splitlines()]
        self.assertTrue(calls)
        self.assertTrue(all(call[:4] == ['-p', 'atlas-work', 'config', 'set'] for call in calls))
        self.assertNotIn('test-model-secret', json.dumps(calls))
        if shutil.which('systemd-analyze'):
            checked = subprocess.run(['systemd-analyze', '--user', 'verify', str(unit)],
                                     capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stderr)
    def test_invalid_inputs_are_preflighted_without_writes(self):
        original = self.config.read_bytes()
        cases = [
            ('--profile-dir', self.home / '.hermes/profiles/atlas'),
            ('--profile-dir', self.home / '.hermes/profiles/default'),
            ('--profile-dir', self.home / '.hermes/profiles/phonebot'),
            ('--profile-dir', self.home / '.hermes/profiles/jyoder'),
            ('--profile-dir', self.home / '.hermes/profiles/not-created-work'),
            ('--profile-dir', 'relative'),
            ('--unit-dir', 'relative'),
            ('--bridge-env', self.home / 'missing/bridge.env'),
            ('--bridge-env', self.profile / '.env'),
            ('--hermes-executable', 'relative'),
            ('--hermes-executable', '/does/not/exist'),
            ('--port', '8642'), ('--port', '8643'), ('--port', '8644'),
            ('--port', '9010'), ('--port', '1023'), ('--port', '65536'),
            ('--unit-dir', str(self.units) + '/%n'),
        ]
        for args in cases:
            with self.subTest(args=args):
                result = self.run_setup(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.config.read_bytes(), original)
                self.assertFalse((self.home / 'calls.jsonl').exists())
                self.assertFalse(list(self.units.iterdir()))

    def test_missing_explicit_model_credential_does_not_use_shell_or_default(self):
        self.private(self.profile / '.env', 'FIRECRAWL_API_KEY=test-web-secret\n')
        self.private(self.home / '.hermes/.env', 'NOUS_API_KEY=default-secret-never-copy\n')
        before = self.config.read_bytes()
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('NOUS_API_KEY', result.stderr)
        self.assertNotIn('default-secret-never-copy', result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.home / 'calls.jsonl').exists())

    def test_insecure_and_linked_inputs_fail_closed(self):
        for target in (self.profile, self.units, self.bridge.parent,
                       self.config, self.profile / '.env', self.bridge):
            with self.subTest(target=target):
                mode = target.stat().st_mode & 0o777
                target.chmod(mode | 0o044)
                result = self.run_setup()
                target.chmod(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.home / 'calls.jsonl').exists())
        for target in (self.config, self.profile / '.env', self.bridge):
            with self.subTest(symlink=target):
                saved = target.with_name(target.name + '.saved')
                target.rename(saved)
                target.symlink_to(saved)
                result = self.run_setup()
                target.unlink()
                saved.rename(target)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.home / 'calls.jsonl').exists())

    def test_missing_model_rejected(self):
        self.private(self.config, 'model: {}\n')
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('model', result.stderr)
        self.assertFalse((self.home / 'calls.jsonl').exists())

    def test_existing_unmanaged_unit_is_not_overwritten(self):
        unit = self.units / 'hermes-atlas-work.service'
        self.private(unit, 'KEEP\n')
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(unit.read_text(), 'KEEP\n')
        self.assertFalse((self.home / 'calls.jsonl').exists())

    def test_all_non_api_platforms_explicitly_disabled(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        platforms = yaml.safe_load(self.config.read_text())['platforms']
        for name in ('local', 'telegram', 'discord', 'whatsapp', 'whatsapp_cloud',
                     'slack', 'signal', 'mattermost', 'matrix', 'homeassistant',
                     'email', 'sms', 'dingtalk', 'webhook', 'msgraph_webhook',
                     'feishu', 'wecom', 'wecom_callback', 'weixin', 'bluebubbles',
                     'qqbot', 'yuanbao', 'relay', 'ntfy', 'raft', 'simplex', 'irc',
                     'google_chat', 'a2a', 'photon', 'buzz', 'line', 'teams'):
            self.assertIs(platforms.get(name, {}).get('enabled'), False, name)

    def test_unit_discards_inherited_user_manager_environment(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        import shlex
        unit = (self.units / 'hermes-atlas-work.service').read_text()
        command = shlex.split(next(line.split('=', 1)[1] for line in unit.splitlines()
                                  if line.startswith('ExecStart=')))
        launcher = command.index(str(self.executable))
        probe = command[:launcher] + [sys.executable, '-c',
            'import os,json; print(json.dumps(dict(os.environ)))']
        inherited = {'HOME': '/wrong', 'PATH': '/wrong', 'HERMES_HOME': '/wrong',
                     'MATTERMOST_TOKEN': 'inherited-secret', 'HASS_TOKEN': 'inherited-secret',
                     'WECOM_BOT_ID': 'inherited-secret', 'BLUEBUBBLES_PASSWORD': 'inherited-secret',
                     'GATEWAY_MULTIPLEX_PROFILES': 'true', 'API_SERVER_HOST': '0.0.0.0',
                     'HERMES_SAFE_MODE': '0', 'PYTHONPATH': '/wrong'}
        checked = subprocess.run(probe, env=inherited, capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        effective = json.loads(checked.stdout)
        self.assertNotIn('MATTERMOST_TOKEN', effective)
        self.assertNotIn('HASS_TOKEN', effective)
        self.assertNotIn('WECOM_BOT_ID', effective)
        self.assertNotIn('BLUEBUBBLES_PASSWORD', effective)
        self.assertNotIn('PYTHONPATH', effective)
        self.assertEqual(effective['HERMES_HOME'], str(self.profile))
        self.assertEqual(effective['HOME'], str(self.home))
        self.assertEqual(effective['HERMES_SAFE_MODE'], '1')
        self.assertEqual(effective.get('HERMES_MANAGED_DIR'), '/etc/hermes')
        self.assertIn('ExecStartPre=/usr/bin/test ! -e /etc/hermes', unit)

    def test_runtime_controls_use_supported_names(self):
        self.private(self.profile / '.env', 'NOUS_API_KEY=test\nHERMES_SAFE_MODE=0\n')
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        env = self.env_values(self.profile / '.env')
        self.assertEqual(env['HERMES_SAFE_MODE'], '1')
        self.assertNotIn('HERMES_SINGLE_PROFILE', env)
        self.assertNotIn('HERMES_GATEWAY_PORT', env)

    def test_ambiguous_platform_overlays_fail_before_writes(self):
        original = self.config.read_text()
        for overlay in (
            {'gateway': {'mattermost': {'enabled': True}}},
            {'gateway': {'future_adapter': {'enabled': True}}},
            {'platforms': {'future_adapter': {'enabled': False}}},
            {'secrets': {'onepassword': {'enabled': True}}},
        ):
            with self.subTest(overlay=overlay):
                c = yaml.safe_load(original)
                c.update(overlay)
                self.private(self.config, yaml.safe_dump(c))
                before = self.config.read_bytes()
                result = self.run_setup()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.config.read_bytes(), before)
                self.assertFalse((self.home / 'calls.jsonl').exists())
        self.private(self.config, original)
        for name in ('gateway.json', '.op.env'):
            with self.subTest(file=name):
                target = self.profile / name
                self.private(target, '{}\n')
                result = self.run_setup()
                self.assertNotEqual(result.returncode, 0)
                target.unlink()

    def test_actual_hermes_loaders_block_inherited_and_source_dotenv_platforms(self):
        source = os.environ.get('HERMES_TEST_SOURCE')
        if not source:
            self.skipTest('Set HERMES_TEST_SOURCE to the reviewed Hermes checkout for offline integration')
        source = Path(source).resolve()
        self.assertTrue((source / 'gateway/config_env.py').is_file())
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        # Actual loader, synthetic project .env only: never load the checkout's real .env.
        probe = r'''
import json, logging, os, pathlib, socket, sys
sys.path.insert(0, sys.argv[1])
def deny_network(*a, **kw):
    raise AssertionError('offline test attempted network')
socket.socket.connect = deny_network
socket.socket.bind = deny_network
logging.disable(logging.CRITICAL)
from gateway.config import Platform, load_gateway_config
from gateway.config_env import _ENV_ENABLE_CREDENTIALS, _ENV_STEPS, _Cred
from hermes_cli.env_loader import load_hermes_dotenv
home = pathlib.Path(os.environ['HERMES_HOME'])
creds = {name: 'synthetic-platform-credential' for names in _ENV_ENABLE_CREDENTIALS.values() for name in names}
for step in _ENV_STEPS:
    if isinstance(step, _Cred):
        for group in step.creds:
            for name in ((group,) if isinstance(group, str) else group):
                creds[name] = 'synthetic-platform-credential'
creds.update(WHATSAPP_ENABLED='true', WEBHOOK_ENABLED='true', MSGRAPH_WEBHOOK_ENABLED='true',
             HERMES_SAFE_MODE='0', GATEWAY_MULTIPLEX_PROFILES='true',
             GATEWAY_RELAY_ALLOW_DIRECT_PLATFORMS='true')
project = home.parent.parent.parent / 'synthetic-checkout.env'
project.write_text(''.join(k + '=' + v + '\n' for k,v in creds.items()))
# Exercise inherited credentials separately from source-checkout fallback credentials.
for inherited in (True, False):
    for name in creds:
        os.environ.pop(name, None)
    if inherited:
        os.environ.update(creds)
    loaded = load_hermes_dotenv(hermes_home=home, project_env=None if inherited else project,
                                load_external_secrets=False)
    assert os.environ['MATTERMOST_TOKEN'] == 'synthetic-platform-credential'
    assert os.environ['HASS_TOKEN'] == 'synthetic-platform-credential'
    assert os.environ['HERMES_SAFE_MODE'] == '1'
    if not inherited:
        assert project in loaded  # prove the fallback was actually exercised
    # Positive control: the exact loader and dummy credentials enable these
    # adapters without the explicit disables (so this is not an empty env test).
    import yaml
    saved = (home / 'config.yaml').read_text()
    unprotected = yaml.safe_load(saved)
    unprotected.pop('platforms')
    (home / 'config.yaml').write_text(yaml.safe_dump(unprotected))
    try:
        unsafe = load_gateway_config()
        enabled = {p.value for p,c in unsafe.platforms.items() if c.enabled}
        assert {'mattermost', 'homeassistant', 'wecom', 'wecom_callback', 'bluebubbles'} <= enabled
    finally:
        (home / 'config.yaml').write_text(saved)
    cfg = load_gateway_config()
    supported = {p.value for p in Platform} | Platform._scan_bundled_plugin_platforms()
    assert supported == {p.value for p in cfg.platforms}, 'platform inventory changed: review required'
    assert {p.value for p,c in cfg.platforms.items() if c.enabled} == {'api_server'}
    assert cfg.multiplex_profiles is False
    api = cfg.platforms[Platform.API_SERVER]
    assert api.extra['host'] == '127.0.0.1'
    assert api.extra['port'] == 8645
print('actual Hermes: inherited and source fallback each API-only; complete platform inventory matched')
'''
        checked = subprocess.run([sys.executable, '-c', probe, str(source)],
            env={'HOME': str(self.home), 'HERMES_HOME': str(self.profile),
                 'PATH': '/usr/bin:/bin', 'HERMES_SAFE_MODE': '1',
                 'HERMES_MANAGED_DIR': str(self.units)},
            cwd=self.home, capture_output=True, text=True, timeout=60)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn('complete platform inventory matched', checked.stdout)

    def test_disabled_api_is_enabled_without_enabling_other_platforms(self):
        c = yaml.safe_load(self.config.read_text())
        c['platforms'] = {'api_server': {'enabled': False}, 'telegram': {'enabled': False}}
        self.private(self.config, yaml.safe_dump(c))
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        platforms = yaml.safe_load(self.config.read_text())['platforms']
        self.assertTrue(platforms['api_server']['enabled'])
        self.assertFalse(platforms['telegram']['enabled'])

    def test_cli_cannot_introduce_a_late_platform_overlay(self):
        text = self.executable.read_text()
        self.executable.write_text(text.replace(
            'p.write_text(yaml.safe_dump(c))',
            "c.setdefault('gateway', {})['mattermost'] = {'enabled': True}\n"
            'p.write_text(yaml.safe_dump(c))'))
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('API_SERVER_KEY', self.env_values(self.profile / '.env'))
        self.assertFalse(list(self.units.iterdir()))

    def test_successful_cli_that_did_not_apply_policy_is_rejected(self):
        self.executable.write_text('#!/bin/sh\nexit 0\n')
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('API_SERVER_KEY', self.env_values(self.profile / '.env'))
        self.assertFalse(list(self.units.iterdir()))

    def test_malformed_or_weak_token_is_not_distributed(self):
        for value in ('tiny', '"' + 'x' * 48 + ' bad"'):
            self.private(self.profile / '.env', 'NOUS_API_KEY=test\nAPI_SERVER_KEY=' + value + '\n')
            result = self.run_setup()
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.home / 'calls.jsonl').exists())
            self.assertNotIn(value.strip('"'), result.stdout + result.stderr)

    def test_other_enabled_platform_is_rejected_not_silently_shared(self):
        c = yaml.safe_load(self.config.read_text())
        c['platforms'] = {'telegram': {'enabled': True}}
        self.private(self.config, yaml.safe_dump(c))
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / 'calls.jsonl').exists())

    def test_messaging_credentials_are_rejected_without_echo(self):
        for name in ('TELEGRAM_BOT_TOKEN', 'MATTERMOST_TOKEN', 'HASS_TOKEN',
                     'WECOM_SECRET', 'WECOM_CALLBACK_CORP_SECRET',
                     'BLUEBUBBLES_PASSWORD', 'NTFY_TOKEN', 'FUTURE_PLATFORM_TOKEN'):
            with self.subTest(name=name):
                self.private(self.profile / '.env',
                             'NOUS_API_KEY=test\n' + name + '=messaging-secret\n')
                result = self.run_setup()
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('messaging-secret', result.stdout + result.stderr)
                self.assertFalse((self.home / 'calls.jsonl').exists())

    def test_env_and_config_data_are_not_used_as_code(self):
        marker = self.home / 'should-not-exist'
        with self.bridge.open('a') as stream:
            stream.write('UNRELATED="$(touch ' + str(marker) + ')"\n')
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        self.assertIn('$(touch', self.bridge.read_text())

    def test_cli_error_output_is_not_leaked(self):
        self.executable.write_text('#!/bin/sh\nprintf "secret-diagnostic" >&2\nexit 1\n')
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('secret-diagnostic', result.stdout + result.stderr)
        self.assertFalse(list(self.units.iterdir()))

    def test_alternate_port_and_missing_bridge_file(self):
        self.bridge.unlink()
        result = self.run_setup('--port', '8650')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.env_values(self.profile / '.env')['API_SERVER_PORT'], '8650')
        self.assertIn('HERMES_ATLAS_WORK_API_KEY', self.env_values(self.bridge))

    def test_required_arguments_have_no_implicit_runtime_defaults(self):
        result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True,
                                env={'HOME': str(self.home), 'PATH': os.environ['PATH']})
        self.assertEqual(result.returncode, 2)
        for option in ('--profile-dir', '--hermes-executable', '--unit-dir', '--bridge-env'):
            self.assertIn(option, result.stderr)

    def test_symlink_ancestor_and_hardlink_refused(self):
        original = self.profile.parent
        moved = original.with_name('profiles-real')
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / 'calls.jsonl').exists())
        original.unlink()
        moved.rename(original)
        os.link(self.bridge, self.home / 'bridge-hardlink')
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / 'calls.jsonl').exists())

    def test_rerun_preserves_token_and_recovers_bridge_copy(self):
        first = self.run_setup()
        self.assertEqual(first.returncode, 0, first.stderr)
        original = self.env_values(self.profile / '.env')['API_SERVER_KEY']
        self.private(self.bridge, '# lost worker token\nHERMES_ATLAS_API_KEY=keep\n')
        second = self.run_setup()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.env_values(self.profile / '.env')['API_SERVER_KEY'], original)
        self.assertEqual(self.env_values(self.bridge)['HERMES_ATLAS_WORK_API_KEY'], original)
        self.assertNotIn(original, second.stdout + second.stderr)


if __name__ == '__main__':
    unittest.main()
