#!/usr/bin/env python3
"""Run the unmodified bridge with a separate deployment config/media root."""
import argparse
import asyncio
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--check', action='store_true', help='Load config only; no network or speech calls')
    args = parser.parse_args()
    if not args.runtime.is_absolute():
        parser.error('--runtime must be absolute')
    from dotenv import load_dotenv
    from phonesys.config import load_config
    load_dotenv(args.runtime / 'bridge.env', override=False)
    config = load_config(args.runtime / 'config.yaml')
    if args.check:
        print('Config loaded offline: ' + ', '.join(f'{n}={e.profile}' for n, e in config.extensions.items()))
        return
    from phonesys import main as bridge
    # Upstream currently has no --config flag and resolves both config and media
    # from ROOT. The generated runtime/media symlink retains PBX bind-mount paths.
    bridge.ROOT = args.runtime
    asyncio.run(bridge.main())


if __name__ == '__main__':
    main()
