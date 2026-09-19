"""Offline tests; never invoke Docker, connect to PBX, or use real credentials."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("pbx_prepare", ROOT / "prepare.py")
assert spec is not None and spec.loader is not None
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pbx = self.root / "pbx"
        self.bridge = self.root / "bridge with spaces"
        self.old_umask = os.umask(0o022)
        self.addCleanup(os.umask, self.old_umask)

    def run_prepare(self, apply):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            prepare.prepare(self.pbx, self.bridge, apply)
        return output.getvalue()

    def test_dry_run_writes_nothing(self):
        self.assertIn("DRY RUN", self.run_prepare(False))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_private_files_and_no_credential_output(self):
        output = self.run_prepare(True)
        for name in ("mysql_root_password.txt", "freepbxuser_password.txt"):
            p = self.pbx / "secrets" / name
            self.assertRegex(p.read_text(), r"^[0-9a-f]{64}\n$")
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            self.assertNotIn(p.read_text().strip(), output)
        self.assertEqual((self.pbx / "secrets/sasl_passwd.txt").read_bytes(), b"")
        self.assertEqual(stat.S_IMODE((self.pbx / "secrets").stat().st_mode), 0o700)
        for name in ("sounds", "rec", "moh/phonesys_hold"):
            self.assertEqual(stat.S_IMODE((self.bridge / "media" / name).stat().st_mode), 0o700)

    def test_repeat_refuses_without_rotation(self):
        self.run_prepare(True)
        p = self.pbx / "secrets/freepbxuser_password.txt"
        before = p.read_bytes()
        with self.assertRaises(ValueError):
            self.run_prepare(True)
        self.assertEqual(p.read_bytes(), before)

    def test_existing_media_fails_before_secrets(self):
        (self.bridge / "media/rec").mkdir(parents=True)
        with self.assertRaises(ValueError):
            self.run_prepare(True)
        self.assertFalse(self.pbx.exists())

    def test_symlink_refused(self):
        self.pbx.symlink_to(self.bridge, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.run_prepare(True)
        self.assertFalse(self.bridge.exists())

    def test_installer_dry_run_never_invokes_docker(self):
        # If called, this fake would fail and create evidence of the violation.
        bindir = self.root / "bin"
        bindir.mkdir()
        fake = bindir / "docker"
        fake.write_text("#!/bin/sh\nexit 99\n")
        fake.chmod(0o700)
        env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"])
        result = subprocess.run(["bash", str(ROOT / "install-fresh.sh")],
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Dry run", result.stdout)

    def test_routing_and_database_contract(self):
        sql = (ROOT / "init.sql").read_text()
        self.assertIn("ON `asterisk`.* TO 'freepbxuser'@'%'", sql)
        self.assertIn("ON `asteriskcdrdb`.* TO 'freepbxuser'@'%'", sql)
        self.assertNotIn("ON *.*", sql)
        dialplan = (ROOT / "extensions_custom.conf.example").read_text()
        for extension in (7000, 7001, 7002, 7003):
            self.assertEqual(dialplan.count(f"Stasis(phonesys,{extension})"), 1)


if __name__ == "__main__":
    unittest.main()
