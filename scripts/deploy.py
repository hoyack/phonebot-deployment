#!/usr/bin/env python3
"""Fresh deployment preparation; default dry-run, never activates services."""
import argparse
import importlib.util
import json
import os
import re
from pathlib import Path
import sys

# Even a plan must not leave __pycache__ behind in a reviewed checkout.
sys.dont_write_bytecode = True
import setup as bundles

ROOT = Path(__file__).resolve().parents[1]
OVERVIEW = '''Prerequisites for telephone activation: an existing Hermes installation,
a deployed FreePBX with ARI/AudioSocket, and a functional SIP trunk for PSTN calls.
Internal-only calls do not require a carrier. Model, Deepgram and ElevenLabs
credentials, reviewed networking and per-profile authentication are also required.

DRY RUN: nothing changed. Select a helper (each defaults to a no-write plan):
  pbx       Prepare digest-pinned FreePBX/MariaDB project (optional fresh PBX)
  profiles  Generate receptionist phonebot plus PIN-protected jyoder and atlas
  stage     Validate and stage the reviewed profile bundle, without activation
Read docs/free-pbx-deploy.md and docs/SETUP.md before explicit --apply.
No packages, services, firewall rules, provider requests or calls are automated.'''


def new_output(out, repo):
    for path in (out, repo):
        bundles.safe_path(path)
    if any(out == root or root in out.parents for root in (ROOT, repo)):
        raise ValueError('private output must be outside the repository')
    if out.exists():
        raise ValueError('refusing existing output; use a new private directory')
    if not out.parent.is_dir():
        raise ValueError('output parent must already exist')


def profiles(args):
    new_output(args.output, args.repo)
    for path in (args.home, args.hermes_bin):
        bundles.safe_path(path)
    if not args.home.is_dir():
        raise ValueError('target home must already exist')
    if not args.hermes_bin.is_file() or not os.access(args.hermes_bin, os.X_OK):
        raise ValueError('existing executable Hermes required; use its canonical path')
    if not (args.repo / 'phonesys/config.yaml').is_file():
        raise ValueError('repo must contain phonesys/config.yaml')
    if args.apply:
        bundles.generate(args)
    else:
        print('DRY RUN: generate private phonebot, jyoder and atlas review bundle; no writes.')


def pbx(args):
    new_output(args.output, ROOT)
    bundles.safe_path(args.phonesys_dir)
    if not args.phonesys_dir.is_dir():
        raise ValueError('phonesys-dir must be an existing dedicated bridge directory')
    for image, repository in ((args.pbx_image, 'escomputers/freepbx'), (args.db_image, 'mariadb')):
        if not re.fullmatch(re.escape(repository) + r'@sha256:[0-9a-f]{64}', image):
            raise ValueError('supply reviewed escomputers/freepbx and mariadb sha256 image digests')
    spec = importlib.util.spec_from_file_location('pbx_prepare', ROOT / 'freepbx/prepare.py')
    assert spec is not None and spec.loader is not None
    preparer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preparer)
    # All existing-media and symlink guards run before creating the output.
    preparer.prepare(args.output, args.phonesys_dir, False)
    names = ('docker-compose.yml', 'my.cnf', 'init.sql', 'install-fresh.sh',
             'extensions_custom.conf.example', 'ari.conf.example', 'musiconhold_custom.conf.example')
    contents = {name: (ROOT / 'freepbx' / name).read_text() for name in names}
    for old, new in (('mariadb:10.11', args.db_image),
                     ('escomputers/freepbx:17-nofail2ban', args.pbx_image)):
        needle = 'image: ' + old
        if contents['docker-compose.yml'].count(needle) != 1:
            raise ValueError('Compose image template changed; review helper before proceeding')
        contents['docker-compose.yml'] = contents['docker-compose.yml'].replace(needle, 'image: ' + new)
    if not args.apply:
        print('DRY RUN: copy digest-pinned Compose and fresh installer; no Docker commands or writes.')
        return
    args.output.mkdir(mode=0o700)
    for name, text in contents.items():
        bundles.write(args.output / name, text)
    bundles.write(args.output / '.env', 'PHONESYS_DIR=' + json.dumps(str(args.phonesys_dir), ensure_ascii=False) + '\nPBX_ADMIN_BIND=127.0.0.1\n')
    preparer.prepare(args.output, args.phonesys_dir, True)
    print('Prepared private PBX project. Review subnet/ports/firewall/media ACLs before manual activation.')
    print('Continue at docs/free-pbx-deploy.md; no containers started or installer executed.')


def stage(args):
    if args.apply and not args.prerequisites_reviewed:
        raise ValueError('review Hermes, FreePBX, functional SIP trunk (PSTN), credentials and networking; pass --prerequisites-reviewed')
    bundles.validate(args)
    args.dry_run = not args.apply
    bundles.stage(args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action')
    profile = sub.add_parser('profiles', help='Prepare three profiles, keys, PINs and units')
    for name in ('home', 'repo', 'hermes-bin', 'output'):
        profile.add_argument('--' + name, type=Path, required=True)
    profile.add_argument('--apply', action='store_true', help='Create fresh local files only')
    container = sub.add_parser('pbx', help='Prepare an external fresh container project')
    for name in ('output', 'phonesys-dir'):
        container.add_argument('--' + name, type=Path, required=True)
    for name in ('pbx-image', 'db-image'):
        container.add_argument('--' + name, required=True)
    container.add_argument('--apply', action='store_true', help='Create fresh local files only')
    staging = sub.add_parser('stage', help='Validate and stage profiles/units without activation')
    for name in ('bundle', 'home'):
        staging.add_argument('--' + name, type=Path, required=True)
    staging.add_argument('--apply', action='store_true', help='Copy files to a fresh home only')
    staging.add_argument('--prerequisites-reviewed', action='store_true',
                         help='Operator acknowledgement, not an automated connectivity test')
    args = parser.parse_args()
    if not args.action:
        print(OVERVIEW)
        return
    try:
        if args.apply:
            os.umask(0o077)
        {'profiles': profiles, 'pbx': pbx, 'stage': stage}[args.action](args)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, f'Preparation stopped: {exc}\n')


if __name__ == '__main__':
    main()
