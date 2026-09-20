#!/usr/bin/env python3
"""Install only the phone-work skill, into an explicitly selected Atlas home."""
import argparse
import json
from pathlib import Path
import shutil
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-home", required=True, type=Path)
    parser.add_argument("--socket-dir", required=True, type=Path)
    args = parser.parse_args()
    home = args.profile_home
    if not home.is_absolute() or home.name != "atlas" or not (home / "config.yaml").is_file():
        parser.error("--profile-home must explicitly name an existing absolute Atlas profile home")
    if not args.socket_dir.is_absolute() or len(str(args.socket_dir).encode()) > 80:
        parser.error("--socket-dir must be absolute and at most 80 bytes (Unix socket path limit)")
    source = Path(__file__).resolve().parents[1] / "skills/atlas-native-work"
    skills = home / "skills"
    if skills.is_symlink():
        parser.error("Refusing a symlinked profile skills directory")
    destination = skills / "atlas-native-work"
    if destination.exists():
        parser.error("Skill already exists; review and explicitly remove/archive that copy before upgrading")
    skills.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".native-work-install-", dir=skills) as temporary:
        staged = Path(temporary) / "atlas-native-work"
        shutil.copytree(source, staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (staged / "scripts/settings.json").write_text(json.dumps({"socket_dir": str(args.socket_dir)}) + "\n")
        staged.rename(destination)
    print(f"Installed {destination}; profile config and credentials unchanged")


if __name__ == "__main__":
    main()
