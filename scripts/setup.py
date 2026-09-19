#!/usr/bin/env python3
"""Fresh PhoneSys bundles. Never starts services, runs Hermes or invokes Docker."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import tarfile

PROFILES = {'phonebot': 8642, 'jyoder': 8643, 'atlas': 8644}
ROOT = Path(__file__).resolve().parents[1]


def quoted(value, execution=False):
    """systemd double-quoted argument (ExecStart expands dollars separately)."""
    value = str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    return '"' + (value.replace('$', '$$') if execution else value) + '"'


def safe_path(path):
    if not path.is_absolute() or '..' in path.parts or any(ord(c) < 32 or ord(c) == 127 for c in str(path)):
        raise ValueError('paths must be absolute, without traversal or control characters')
    if any(c in str(path) for c in '$%"\\'):
        raise ValueError('paths containing dollar, percent, double quote or backslash are not supported by this unit renderer')
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            raise ValueError(f'symlink path refused: {parent}')


def inspect_bundle(bundle):
    safe_path(bundle)
    for path in [bundle, *bundle.rglob('*')]:
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError('bundle must contain only regular files and directories')
        if path.stat().st_mode & 0o077:
            raise ValueError(f'bundle permissions must be private: {path}')


def env_values(path):
    values = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep:
            raise ValueError('invalid env line (expected NAME=value)')
        values[key] = value.strip().strip('"').strip("'")
    return values


def validate(args):
    inspect_bundle(args.bundle)
    values = env_values(args.bundle / 'bridge.env')
    missing = [key for key in ('ARI_USER', 'ARI_PASSWORD', 'DEEPGRAM_API_KEY', 'ELEVENLABS_API_KEY', 'JOHN_YODER_PIN', 'ATLAS_PIN') if not values.get(key)]
    if missing:
        raise ValueError('missing credentials: ' + ', '.join(missing))
    keys = []
    for name in PROFILES:
        key = env_values(args.bundle / 'profiles' / name / '.env').get('API_SERVER_KEY', '')
        if len(key) < 32 or key != values.get(f'HERMES_{name.upper()}_API_KEY'):
            raise ValueError(f'API key mismatch or weak key: {name}')
        keys.append(key)
    if len(set(keys)) != len(keys):
        raise ValueError('profiles require separate API keys')
    for name in ('JOHN_YODER_PIN', 'ATLAS_PIN'):
        if not values[name].isascii() or not values[name].isdigit() or len(values[name]) < 6:
            raise ValueError(f'{name} must be at least six ASCII digits')
    print('Bundle credentials and private permissions validated offline; no provider or call checks performed.')


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('x', encoding='utf-8') as stream:
        os.chmod(path, 0o600)
        stream.write(text)


def unit(description, home, cwd, command, envfile, extra=''):
    return f'''[Unit]
Description={description}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={str(cwd).replace('%', '%%')}
Environment={quoted('HOME=' + str(home))}
EnvironmentFile={str(envfile).replace('%', '%%')}
{extra}ExecStart={command}
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=default.target
'''


def generate(args):
    out, home, repo = args.output, args.home, args.repo
    for path in (out, home, repo, args.hermes_bin):
        safe_path(path)
    if not (repo / 'phonesys/config.yaml').is_file():
        raise ValueError('repo must contain phonesys/config.yaml')
    if repo == out or repo in out.parents or ROOT == out or ROOT in out.parents:
        raise ValueError('secret output must be outside the repository')
    out.mkdir(mode=0o700)
    bridge = {'ARI_USER': 'phonesys', 'ARI_PASSWORD': secrets.token_hex(32),
              'DEEPGRAM_API_KEY': '', 'ELEVENLABS_API_KEY': '',
              'JOHN_YODER_PIN': ''.join(secrets.choice('0123456789') for _ in range(8)),
              'ATLAS_PIN': ''.join(secrets.choice('0123456789') for _ in range(8))}
    for name, port in PROFILES.items():
        key = secrets.token_hex(32)
        bridge[f'HERMES_{name.upper()}_API_KEY'] = key
        target = home / '.hermes/profiles' / name
        write(out / 'profiles' / name / '.env', f'API_SERVER_KEY={key}\n')
        write(out / 'profiles' / name / 'profile.yaml', f'description: {name} telephone assistant\ndescription_auto: false\n')
        soul = (ROOT / 'phonebot/SOUL.md').read_text() if name == 'phonebot' else f'# {name}\nYou are the {name} telephone assistant. Give concise spoken answers. Never claim an action succeeded without evidence.\n'
        write(out / 'profiles' / name / 'SOUL.md', soul)
        extra = (f'Environment={quoted("HERMES_HOME=" + str(target))}\n'
                 f'Environment=API_SERVER_ENABLED=true API_SERVER_HOST=127.0.0.1 API_SERVER_PORT={port}\n')
        write(out / 'units' / f'hermes-{name}.service', unit(
            f'Hermes {name} standalone voice gateway', home, target,
            f'{quoted(args.hermes_bin, True)} gateway run', target / '.env', extra))
    write(out / 'bridge.env', ''.join(f'{key}={value}\n' for key, value in bridge.items()))
    runtime = home / '.config/phonesys'
    write(out / 'units/phonesys-bridge.service', unit(
        'PhoneSys ARI voice bridge', home, repo / 'phonesys',
        f'{quoted(repo / "phonesys/.venv/bin/python", True)} {quoted(repo / "scripts/run-bridge.py", True)} --runtime {quoted(runtime, True)}',
        runtime / 'bridge.env'))
    config = (repo / 'phonesys/config.yaml').read_text()
    # Deliberate fresh-install divergence: John must not reuse Mary's key.
    before, john = config.split('  "7002":', 1)
    john, after = john.split('  "7003":', 1)
    john = john.replace('hermes_api_key_env: HERMES_PHONEBOT_API_KEY', 'hermes_api_key_env: HERMES_JYODER_API_KEY')
    write(out / 'config.yaml', before + '  "7002":' + john + '  "7003":' + after)
    write(out / 'manifest.json', json.dumps({'home': str(home), 'repo': str(repo), 'hermes_bin': str(args.hermes_bin)}, indent=2) + '\n')
    print('Generated private bundle; nothing installed or started.')


def stage(args):
    bundle, home = args.bundle, args.home
    inspect_bundle(bundle)
    safe_path(home)
    manifest = json.loads((bundle / 'manifest.json').read_text())
    if manifest['home'] != str(home):
        raise ValueError('bundle target home does not match --home; regenerate')
    targets = [(bundle / 'profiles' / name, home / '.hermes/profiles' / name) for name in PROFILES]
    targets += [(bundle / 'units' / name, home / '.config/systemd/user' / name)
                for name in [*(f'hermes-{p}.service' for p in PROFILES), 'phonesys-bridge.service']]
    required = [bundle / 'bridge.env', bundle / 'config.yaml']
    for source, _ in targets:
        required.extend([source / name for name in ('.env', 'SOUL.md', 'profile.yaml')] if source.is_dir() else [source])
    for source in required:
        if not source.is_file():
            raise ValueError(f'incomplete bundle: {source}')
    runtime = home / '.config/phonesys'
    for _, target in targets + [(None, runtime)]:
        safe_path(target)
        if target.exists() or target.is_symlink():
            raise ValueError(f'refusing existing target: {target}')
    for source, target in targets:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            write(target, source.read_text())
    runtime.mkdir(mode=0o700)
    for name in ('bridge.env', 'config.yaml'):
        write(runtime / name, (bundle / name).read_text())
    (runtime / 'media').symlink_to(Path(manifest['repo']) / 'phonesys/media', target_is_directory=True)
    print('Staged new files only; configure each profile and activate manually. No services started.')


def backup(args):
    safe_path(args.home)
    safe_path(args.output)
    if args.output == ROOT or ROOT in args.output.parents:
        raise ValueError('backup must be outside the repository')
    sources = [(args.home / '.hermes/profiles' / p, 'profiles/' + p) for p in PROFILES]
    sources += [(args.home / '.config/phonesys', 'runtime'),
                *((args.home / '.config/systemd/user' / f'hermes-{p}.service', f'units/hermes-{p}.service') for p in PROFILES),
                (args.home / '.config/systemd/user/phonesys-bridge.service', 'units/phonesys-bridge.service')]
    for source, _ in sources:
        safe_path(source)
        if not source.exists():
            raise ValueError(f'missing backup source: {source}')
        if args.output == source or source in args.output.parents:
            raise ValueError('backup output must be outside source trees')
    # No database commands, volume guessing, network or services. Symlinks and
    # special files are excluded, including the runtime/media bind-mount link.
    def regular_only(info):
        return info if info.isfile() or info.isdir() else None
    with args.output.open('xb') as stream:
        os.chmod(args.output, 0o600)
        with tarfile.open(fileobj=stream, mode='w:gz', dereference=False) as archive:
            for source, name in sources:
                archive.add(source, arcname=name, filter=regular_only)
    print('Private local snapshot created. Not a PBX/database backup; encrypt before moving.')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    gen = sub.add_parser('generate', help='Generate a new private bundle; no activation')
    for arg in ('output', 'home', 'repo', 'hermes-bin'):
        gen.add_argument('--' + arg, required=True, type=Path)
    staging = sub.add_parser('stage', help='Copy bundle to explicit fresh home; never overwrite or activate')
    staging.add_argument('--bundle', required=True, type=Path)
    staging.add_argument('--home', required=True, type=Path)
    check = sub.add_parser('validate', help='Offline credential consistency and permission checks; no network')
    check.add_argument('--bundle', required=True, type=Path)
    snap = sub.add_parser('backup', help='Private local profile/config snapshot only; stop writers manually first')
    snap.add_argument('--home', required=True, type=Path)
    snap.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        {'generate': generate, 'stage': stage, 'validate': validate, 'backup': backup}[args.action](args)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, f'error: {exc}\n')


if __name__ == '__main__':
    main()
