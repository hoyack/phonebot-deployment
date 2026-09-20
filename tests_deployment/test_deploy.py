"""Offline deployment entrypoint checks; no Hermes, Docker or services invoked."""
import json
import pytest
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/deploy.py'


def run(tmp_path, *args):
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    return subprocess.run([sys.executable, '-B', str(SCRIPT), *map(str, args)],
                          env={'HOME': str(home), 'PATH': ''},
                          capture_output=True, text=True)


def profile_args(tmp_path):
    hermes = tmp_path / 'hermes'
    hermes.write_text('#!/bin/sh\nexit 99\n')  # Must never execute.
    hermes.chmod(0o700)
    return ['profiles', '--home', tmp_path / 'home', '--repo', ROOT,
            '--hermes-bin', hermes, '--output', tmp_path / 'bundle']


def test_default_and_profiles_plan_do_not_write(tmp_path):
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'functional SIP trunk' in result.stdout
    args = profile_args(tmp_path)
    result = run(tmp_path, *args)
    assert result.returncode == 0, result.stderr
    assert 'DRY RUN' in result.stdout
    assert not (tmp_path / 'bundle').exists()
    assert not list((tmp_path / 'home').iterdir())


def pbx_args(tmp_path):
    source = tmp_path / 'bridge source'
    source.mkdir(exist_ok=True)
    return ['pbx', '--output', tmp_path / 'private pbx', '--phonesys-dir', source,
            '--pbx-image', 'escomputers/freepbx@sha256:' + 'a' * 64,
            '--db-image', 'mariadb@sha256:' + 'b' * 64]


def test_pbx_plan_and_apply_prepare_external_pinned_project(tmp_path):
    args = pbx_args(tmp_path)
    result = run(tmp_path, *args)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / 'private pbx').exists()
    assert not (tmp_path / 'bridge source/media').exists()
    result = run(tmp_path, *args, '--apply')
    assert result.returncode == 0, result.stderr
    out = tmp_path / 'private pbx'
    compose = (out / 'docker-compose.yml').read_text()
    assert 'image: escomputers/freepbx@sha256:' + 'a' * 64 in compose
    assert 'image: mariadb@sha256:' + 'b' * 64 in compose
    assert 'PHONESYS_DIR=' + json.dumps(str(tmp_path / 'bridge source')) in (out / '.env').read_text()
    assert (out / 'secrets/mysql_root_password.txt').stat().st_mode & 0o777 == 0o600
    assert (tmp_path / 'bridge source/media/rec').is_dir()
    assert (out / 'install-fresh.sh').is_file()
    assert run(tmp_path, *args, '--apply').returncode != 0


def test_pbx_preserves_unicode_paths_in_compose_environment(tmp_path):
    args = pbx_args(tmp_path)
    source = tmp_path / 'téléphone source'
    source.mkdir()
    args[4] = source
    result = run(tmp_path, *args, '--apply')
    assert result.returncode == 0, result.stderr
    assert str(source) in (tmp_path / 'private pbx/.env').read_text()


@pytest.mark.parametrize('blocker', ['media', 'symlink', 'unpinned'])
def test_pbx_preflight_refuses_before_any_output(tmp_path, blocker):
    args = pbx_args(tmp_path)
    if blocker == 'media':
        (tmp_path / 'bridge source/media/rec').mkdir(parents=True)
    elif blocker == 'symlink':
        (tmp_path / 'bridge source/media').symlink_to(tmp_path / 'home')
    else:
        args[-1] = 'mariadb:latest'
    result = run(tmp_path, *args, '--apply')
    assert result.returncode != 0
    assert not (tmp_path / 'private pbx').exists()


def test_profiles_apply_reuses_generator_without_running_hermes(tmp_path):
    result = run(tmp_path, *profile_args(tmp_path), '--apply')
    assert result.returncode == 0, result.stderr
    out = tmp_path / 'bundle'
    assert sorted(p.name for p in (out / 'profiles').iterdir()) == ['atlas', 'jyoder', 'phonebot']
    assert (out / 'bridge.env').stat().st_mode & 0o777 == 0o600
    assert 'HERMES_JYODER_API_KEY' in (out / 'config.yaml').read_text()
    assert not list((tmp_path / 'home').iterdir())


def test_stage_requires_credentials_and_acknowledgement_and_defaults_to_plan(tmp_path):
    assert run(tmp_path, *profile_args(tmp_path), '--apply').returncode == 0
    bundle = tmp_path / 'bundle'
    args = ['stage', '--bundle', bundle, '--home', tmp_path / 'home']
    assert run(tmp_path, *args, '--apply', '--prerequisites-reviewed').returncode != 0
    assert not list((tmp_path / 'home').iterdir())
    env = bundle / 'bridge.env'
    text = env.read_text().replace('DEEPGRAM_API_KEY=\n', 'DEEPGRAM_API_KEY=offline-dummy\n')
    env.write_text(text.replace('ELEVENLABS_API_KEY=\n', 'ELEVENLABS_API_KEY=offline-dummy\n'))
    result = run(tmp_path, *args)
    assert result.returncode == 0, result.stderr
    assert not list((tmp_path / 'home').iterdir())
    assert run(tmp_path, *args, '--apply').returncode != 0
    result = run(tmp_path, *args, '--apply', '--prerequisites-reviewed')
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'home/.hermes/profiles/atlas/.env').is_file()
    assert not (tmp_path / 'home/.hermes/config.yaml').exists()
    assert run(tmp_path, *args, '--apply', '--prerequisites-reviewed').returncode != 0


@pytest.mark.parametrize('kind', ['existing', 'repo', 'symlink', 'executable'])
def test_profiles_reject_unsafe_targets(tmp_path, kind):
    args = profile_args(tmp_path)
    if kind == 'existing':
        (tmp_path / 'bundle').mkdir()
    elif kind == 'repo':
        args[-1] = ROOT / 'DO_NOT_CREATE'
    elif kind == 'symlink':
        (tmp_path / 'bundle').symlink_to(tmp_path / 'home')
    else:
        (tmp_path / 'hermes').chmod(0o600)
    result = run(tmp_path, *args, '--apply')
    assert result.returncode != 0
    assert not list((tmp_path / 'home').iterdir())
