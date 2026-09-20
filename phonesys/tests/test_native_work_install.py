import json
from pathlib import Path
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_explicit_profile_install_is_portable_and_non_overwriting(tmp_path):
    installer = ROOT / "scripts/install-atlas-native-work.py"
    assert installer.exists(), "explicit profile skill installer missing"
    profile = tmp_path / "atlas"
    profile.mkdir()
    (profile / "config.yaml").write_text("{}\n")
    socket_dir = tmp_path / "work"
    command = [sys.executable, str(installer), "--profile-home", str(profile), "--socket-dir", str(socket_dir)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    installed = profile / "skills/atlas-native-work"
    assert (installed / "scripts/atlas-work.py").read_bytes() == (ROOT / "skills/atlas-native-work/scripts/atlas-work.py").read_bytes()
    assert json.loads((installed / "scripts/settings.json").read_text()) == {"socket_dir": str(socket_dir)}
    assert (profile / "config.yaml").read_text() == "{}\n"
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0
    assert "already exists" in result.stderr
    result = subprocess.run([sys.executable, str(installer)], capture_output=True, text=True)
    assert result.returncode != 0
    skill = (installed / "SKILL.md").read_text()
    fm = yaml.safe_load(skill.split("---", 2)[1])
    assert len(fm["description"]) <= 60
    assert "/home/" not in skill
    assert "HERMES_SESSION_ID" in skill
    assert "proactive" in skill.lower()
