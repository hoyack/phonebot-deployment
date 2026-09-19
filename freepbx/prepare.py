#!/usr/bin/env python3
"""Prepare local fresh-install files only; never starts Docker or edits a PBX."""
import argparse
import os
from pathlib import Path
import secrets
import stat


def prepare(pbx_dir: Path, phonesys_dir: Path, apply: bool) -> None:
    # Resolve parents but reject symlink targets before creating anything.
    secret_dir = pbx_dir / "secrets"
    targets = [secret_dir / n for n in (
        "mysql_root_password.txt", "freepbxuser_password.txt", "sasl_passwd.txt"
    )]
    media = [phonesys_dir / "media" / n for n in ("sounds", "rec", "moh/phonesys_hold")]
    for path in [secret_dir, *targets, *media]:
        if any(p.is_symlink() for p in [path, *path.parents]):
            raise ValueError(f"Refusing symlink path: {path}")
    for path in targets:
        if path.exists():
            raise ValueError(f"Refusing existing secret (no rotation/overwrite): {path}")
    for path in media:
        if path.exists():
            raise ValueError(f"Fresh setup requires absent dedicated media directory: {path}")
    if secret_dir.exists() and stat.S_IMODE(secret_dir.stat().st_mode) & 0o077:
        raise ValueError(f"First restrict existing secret directory to mode 0700: {secret_dir}")
    print(f"{'CREATE' if apply else 'DRY RUN'}: three secret files under {secret_dir}")
    for path in media:
        print(f"{'CREATE' if apply else 'DRY RUN'}: {path}")
    if not apply:
        print("No changes. Pass --apply to create local files; never starts services.")
        return
    os.umask(0o077)
    secret_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for path in targets:
        # Exclusive creation: even a concurrent setup cannot overwrite credentials.
        with path.open("x") as stream:
            if path.name != "sasl_passwd.txt":
                stream.write(secrets.token_hex(32) + "\n")
    for path in media:
        path.mkdir(parents=True, mode=0o700)
    print("Created private files. Set media group/ACLs AFTER discovering Asterisk IDs.")
    print("SMTP file is empty, not a configured mail account. No ARI/SIP secrets created.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbx-dir", type=Path, default=Path(__file__).absolute().parent)
    parser.add_argument("--phonesys-dir", type=Path, required=True,
                        help="Exact bridge source directory (must match PHONESYS_DIR)")
    parser.add_argument("--apply", action="store_true", help="Create new local files only")
    args = parser.parse_args()
    try:
        prepare(args.pbx_dir.absolute(), args.phonesys_dir.absolute(), args.apply)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Preparation stopped: {exc}\n")


if __name__ == "__main__":
    main()
