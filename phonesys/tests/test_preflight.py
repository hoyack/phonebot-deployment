from phonesys.main import preflight_media_dirs


def test_clean_dirs_report_no_problems(tmp_path):
    sounds, rec = tmp_path / "sounds", tmp_path / "rec"
    sounds.mkdir()
    rec.mkdir()
    # chmod explicitly: mkdir's mode is masked by umask, which is how the real
    # recording directory silently ended up 755 without the group-write bit.
    sounds.chmod(0o755)
    rec.chmod(0o775)
    assert preflight_media_dirs(sounds, rec) == []


def test_recording_dir_without_group_write_is_flagged(tmp_path):
    """The exact misconfiguration that made Asterisk fail silently: the
    recording dir was 755, so only root could write it."""
    sounds, rec = tmp_path / "sounds", tmp_path / "rec"
    sounds.mkdir(mode=0o755)
    rec.mkdir(mode=0o755)
    problems = preflight_media_dirs(sounds, rec)
    assert len(problems) == 1
    assert "group-writable" in problems[0]
    assert "chmod 775" in problems[0]


def test_missing_dir_is_flagged(tmp_path):
    sounds = tmp_path / "sounds"
    sounds.mkdir(mode=0o755)
    problems = preflight_media_dirs(sounds, tmp_path / "absent")
    assert any("does not exist" in p for p in problems)
