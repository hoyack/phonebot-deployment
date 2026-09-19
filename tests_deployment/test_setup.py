import os
from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/setup.py'


def run(tmp_path, *args):
    home = tmp_path / 'isolated home'
    home.mkdir(exist_ok=True)
    env = {'HOME': str(home), 'PATH': os.environ['PATH']}
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], env=env, text=True, capture_output=True)


def generate(tmp_path):
    out = tmp_path / 'private bundle'
    home = tmp_path / 'isolated home'
    result = run(tmp_path, 'generate', '--output', out, '--home', home,
                 '--repo', ROOT, '--hermes-bin', '/opt/hermes with spaces/bin/hermes')
    assert result.returncode == 0, result.stderr
    return out, home


def env_file(path):
    return dict(line.split('=', 1) for line in path.read_text().splitlines() if line and not line.startswith('#'))


def test_generate_private_separate_profile_secrets_without_install(tmp_path):
    out, home = generate(tmp_path)
    assert not list(home.iterdir())
    bridge = env_file(out / 'bridge.env')
    keys = []
    for name, port in [('phonebot', 8642), ('jyoder', 8643), ('atlas', 8644)]:
        profile = out / 'profiles' / name
        key = env_file(profile / '.env')['API_SERVER_KEY']
        keys.append(key)
        assert bridge[f'HERMES_{name.upper()}_API_KEY'] == key
        unit = (out / 'units' / f'hermes-{name}.service').read_text()
        assert f'API_SERVER_PORT={port}' in unit
        assert 'API_SERVER_HOST=127.0.0.1' in unit
        assert '"/opt/hermes with spaces/bin/hermes" gateway run' in unit
        assert (profile / '.env').stat().st_mode & 0o777 == 0o600
    assert len(set(keys)) == 3
    assert all(len(key) >= 40 for key in keys)
    assert len(bridge['ARI_PASSWORD']) >= 40
    assert bridge['JOHN_YODER_PIN'].isdigit() and len(bridge['JOHN_YODER_PIN']) == 8
    assert bridge['ATLAS_PIN'].isdigit() and len(bridge['ATLAS_PIN']) == 8
    assert out.stat().st_mode & 0o777 == 0o700


def test_stage_fresh_bundle_and_refuse_second_stage(tmp_path):
    out, home = generate(tmp_path)
    result = run(tmp_path, 'stage', '--bundle', out, '--home', home)
    assert result.returncode == 0, result.stderr
    assert (home / '.hermes/profiles/jyoder/.env').read_bytes() == (out / 'profiles/jyoder/.env').read_bytes()
    runtime = home / '.config/phonesys'
    assert (runtime / 'config.yaml').exists()
    assert (runtime / 'media').resolve() == ROOT / 'phonesys/media'
    assert not (home / '.hermes/profiles/phonebot/config.yaml').exists()
    assert run(tmp_path, 'stage', '--bundle', out, '--home', home).returncode != 0


def test_bridge_launcher_uses_runtime_root_not_unsupported_config_flag(tmp_path):
    out, home = generate(tmp_path)
    unit = (out / 'units/phonesys-bridge.service').read_text()
    assert 'run-bridge.py" --runtime ' in unit
    assert '-m phonesys.main --config' not in unit


@pytest.mark.parametrize('existing', ['.hermes/profiles/phonebot/.env', '.hermes/profiles/atlas/profile.yaml', '.config/phonesys/bridge.env', '.config/systemd/user/hermes-jyoder.service'])
def test_stage_preflights_all_existing_targets_without_partial_write(tmp_path, existing):
    out, home = generate(tmp_path)
    old = home / existing
    old.parent.mkdir(parents=True)
    old.write_text('KEEP')
    result = run(tmp_path, 'stage', '--bundle', out, '--home', home)
    assert result.returncode != 0
    assert old.read_text() == 'KEEP'
    assert not (home / '.hermes/profiles/jyoder').exists()


def test_symlink_parent_refused(tmp_path):
    out, home = generate(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (home / '.hermes').symlink_to(outside, target_is_directory=True)
    assert run(tmp_path, 'stage', '--bundle', out, '--home', home).returncode != 0
    assert not list(outside.iterdir())


@pytest.mark.parametrize('bad', ['relative', '/tmp/new\nline', '/tmp/$cash', '/tmp/%n', '/tmp/"quoted"'])
def test_invalid_generation_paths_write_nothing(tmp_path, bad):
    out = tmp_path / 'bundle'
    result = run(tmp_path, 'generate', '--output', out, '--home', bad,
                 '--repo', ROOT, '--hermes-bin', '/usr/bin/hermes')
    assert result.returncode != 0
    assert not out.exists()


def test_validate_reports_blank_external_credentials_without_printing_secrets(tmp_path):
    out, home = generate(tmp_path)
    result = run(tmp_path, 'validate', '--bundle', out)
    assert result.returncode != 0
    assert 'DEEPGRAM_API_KEY' in result.stderr
    assert env_file(out / 'bridge.env')['ARI_PASSWORD'] not in result.stdout + result.stderr
    with (out / 'bridge.env').open('a') as f:
        f.write('DEEPGRAM_API_KEY=test-only\nELEVENLABS_API_KEY=test-only\n')
    assert run(tmp_path, 'validate', '--bundle', out).returncode == 0


def test_bundle_symlink_refused_before_stage(tmp_path):
    out, home = generate(tmp_path)
    secret = out / 'profiles/phonebot/.env'
    secret.unlink()
    secret.symlink_to(out / 'bridge.env')
    assert run(tmp_path, 'stage', '--bundle', out, '--home', home).returncode != 0
    assert not list(home.iterdir())


def test_help_and_missing_arguments_do_not_touch_home(tmp_path):
    assert run(tmp_path, '--help').returncode == 0
    assert run(tmp_path).returncode != 0
    assert not list((tmp_path / 'isolated home').iterdir())


def test_launcher_checks_generated_config_without_network(tmp_path):
    out, home = generate(tmp_path)
    env = {'HOME': str(home), 'PATH': os.environ['PATH'], 'PYTHONPATH': str(ROOT / 'phonesys/src')}
    env.update(env_file(out / 'bridge.env'))
    env.update(DEEPGRAM_API_KEY='test', ELEVENLABS_API_KEY='test')
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/run-bridge.py'), '--runtime', str(out), '--check'], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert '7002=jyoder' in result.stdout


def test_backup_is_explicit_private_no_overwrite_and_does_not_follow_links(tmp_path):
    out, home = generate(tmp_path)
    assert run(tmp_path, 'stage', '--bundle', out, '--home', home).returncode == 0
    dest = tmp_path / 'backup.tar.gz'
    result = run(tmp_path, 'backup', '--home', home, '--output', dest)
    assert result.returncode == 0, result.stderr
    assert dest.stat().st_mode & 0o777 == 0o600
    import tarfile
    with tarfile.open(dest) as archive:
        names = archive.getnames()
        assert 'profiles/atlas/.env' in names
        assert 'runtime/media' not in names
    assert run(tmp_path, 'backup', '--home', home, '--output', dest).returncode != 0
    assert run(tmp_path, 'backup', '--home', home, '--output', home / '.config/phonesys/bad.tar.gz').returncode != 0


def test_generation_refuses_repository_secret_output(tmp_path):
    result = run(tmp_path, 'generate', '--output', ROOT / 'tests_deployment/DO_NOT_CREATE', '--home', tmp_path,
                 '--repo', ROOT, '--hermes-bin', '/usr/bin/hermes')
    assert result.returncode != 0
    assert not (ROOT / 'tests_deployment/DO_NOT_CREATE').exists()


def test_incomplete_bundle_never_partially_stages(tmp_path):
    out, home = generate(tmp_path)
    (out / 'units/hermes-atlas.service').unlink()
    assert run(tmp_path, 'stage', '--bundle', out, '--home', home).returncode != 0
    assert not list(home.iterdir())


def test_systemd_accepts_rendered_units_with_spaces(tmp_path):
    import shutil
    if not shutil.which('systemd-analyze'):
        pytest.skip('systemd-analyze is Linux-specific')
    repo = tmp_path / 'repo with spaces'
    (repo / 'phonesys/.venv/bin').mkdir(parents=True)
    shutil.copyfile(ROOT / 'phonesys/config.yaml', repo / 'phonesys/config.yaml')
    shutil.copy('/usr/bin/true', repo / 'phonesys/.venv/bin/python')
    out = tmp_path / 'units bundle'
    home = tmp_path / 'home with spaces'
    result = run(tmp_path, 'generate', '--output', out, '--home', home, '--repo', repo, '--hermes-bin', '/usr/bin/true')
    assert result.returncode == 0, result.stderr
    # No user manager/service activation: this is systemd's static parser.
    result = subprocess.run(['systemd-analyze', 'verify', *map(str, (out / 'units').glob('*.service'))], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert 'EnvironmentFile= path is not absolute' not in result.stderr
