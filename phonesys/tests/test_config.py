import pytest
from pathlib import Path
from phonesys.config import load_config, MissingSecretError


def _write(tmp_path: Path, yaml_text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml_text)
    return p


CONFIG_YAML = """
ari:
  base_url: http://127.0.0.1:8088/ari
  app: phonesys
extensions:
  "7000":
    profile: phonebot
    hermes_base_url: http://127.0.0.1:8642
    hermes_api_key_env: HERMES_PHONEBOT_API_KEY
    greeting: "Hi, this is Hermes."
    voice_id: voice-abc
    max_silence_seconds: 2
    max_turn_seconds: 30
    max_call_seconds: 600
"""


@pytest.mark.parametrize("raw, expected", [("", 30.0), ("    hermes_reply_timeout_seconds: 12.5\n", 12.5)])
def test_hermes_reply_budget_config(tmp_path, monkeypatch, raw, expected):
    monkeypatch.setenv("ARI_USER", "phonesys")
    monkeypatch.setenv("ARI_PASSWORD", "secret")
    monkeypatch.setenv("HERMES_PHONEBOT_API_KEY", "hkey")
    cfg = load_config(_write(tmp_path, CONFIG_YAML + raw))
    assert cfg.extensions["7000"].hermes_reply_timeout_seconds == expected


@pytest.mark.parametrize("raw", ["0", "-1", ".nan", ".inf"])
def test_hermes_reply_budget_rejects_unbounded_values(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("ARI_USER", "phonesys")
    monkeypatch.setenv("ARI_PASSWORD", "secret")
    monkeypatch.setenv("HERMES_PHONEBOT_API_KEY", "hkey")
    with pytest.raises(ValueError, match="hermes_reply_timeout_seconds"):
        load_config(_write(tmp_path, CONFIG_YAML +
                           f"    hermes_reply_timeout_seconds: {raw}\n"))


def test_loads_extension(tmp_path, monkeypatch):
    monkeypatch.setenv("ARI_USER", "phonesys")
    monkeypatch.setenv("ARI_PASSWORD", "secret")
    monkeypatch.setenv("HERMES_PHONEBOT_API_KEY", "hkey")
    cfg = load_config(_write(tmp_path, CONFIG_YAML))
    ext = cfg.extensions["7000"]
    assert ext.profile == "phonebot"
    assert ext.hermes_base_url == "http://127.0.0.1:8642"
    assert ext.hermes_api_key == "hkey"
    assert ext.max_silence_seconds == 2
    assert cfg.ari.username == "phonesys"
    assert cfg.ari.app == "phonesys"


def test_missing_secret_raises_naming_the_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("ARI_USER", "phonesys")
    monkeypatch.setenv("ARI_PASSWORD", "secret")
    monkeypatch.delenv("HERMES_PHONEBOT_API_KEY", raising=False)
    with pytest.raises(MissingSecretError) as exc:
        load_config(_write(tmp_path, CONFIG_YAML))
    assert "HERMES_PHONEBOT_API_KEY" in str(exc.value)


def test_unknown_extension_is_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("ARI_USER", "phonesys")
    monkeypatch.setenv("ARI_PASSWORD", "secret")
    monkeypatch.setenv("HERMES_PHONEBOT_API_KEY", "hkey")
    cfg = load_config(_write(tmp_path, CONFIG_YAML))
    assert "9999" not in cfg.extensions


STREAMING_YAML = CONFIG_YAML + """
  "7001":
    mode: streaming
    profile: phonebot
    hermes_base_url: http://127.0.0.1:8642
    hermes_api_key_env: HERMES_PHONEBOT_API_KEY
    greeting: "Hi, this is Hermes."
    voice_id: voice-abc
    barge_in_confirm_ms: 300
    audiosocket_host: host.docker.internal
    audiosocket_port: 9010
    audiosocket_listen_port: 19010
"""


FILLER_CONFIG_YAML = """
ari:
  base_url: http://127.0.0.1:8088/ari
  app: phonesys
extensions:
  "7000":
    profile: phonebot
    hermes_base_url: http://127.0.0.1:8642
    hermes_api_key_env: HERMES_PHONEBOT_API_KEY
    greeting: "Hi, this is Hermes."
    voice_id: voice-abc
    max_silence_seconds: 2
    max_turn_seconds: 30
    max_call_seconds: 600
    filler_delay_ms: 800
    filler_repeat_ms: 2500
    filler_phrases:
      - ["Let me think.", "One moment."]
      - ["Still working."]
"""


FILLER_HASHABLE_YAML = """
ari:
  base_url: http://127.0.0.1:8088/ari
  app: phonesys
extensions:
  "7000":
    profile: phonebot
    hermes_base_url: http://127.0.0.1:8642
    hermes_api_key_env: HERMES_PHONEBOT_API_KEY
    greeting: "Hi, this is Hermes."
    voice_id: voice-abc
    max_silence_seconds: 2
    max_turn_seconds: 30
    max_call_seconds: 600
    filler_phrases:
      - ["Let me think."]
"""


def test_mode_defaults_to_turn_and_streaming_is_read(tmp_path, monkeypatch):
    monkeypatch.setenv("ARI_USER", "phonesys")
    monkeypatch.setenv("ARI_PASSWORD", "secret")
    monkeypatch.setenv("HERMES_PHONEBOT_API_KEY", "hkey")
    cfg = load_config(_write(tmp_path, STREAMING_YAML))
    assert cfg.extensions["7000"].mode == "turn"
    s = cfg.extensions["7001"]
    assert s.mode == "streaming"
    assert s.barge_in_confirm_ms == 300
    assert s.audiosocket_port == 9010
    assert s.audiosocket_listen_port == 19010


def test_filler_defaults_to_disabled(tmp_path, monkeypatch):
    """An extension that does not opt in must be untouched -- including 7000."""
    monkeypatch.setenv("ARI_USER", "phonesys")
    monkeypatch.setenv("ARI_PASSWORD", "secret")
    monkeypatch.setenv("HERMES_PHONEBOT_API_KEY", "hkey")
    cfg = load_config(_write(tmp_path, CONFIG_YAML))
    ext = cfg.extensions["7000"]
    assert ext.filler_delay_ms == 0
    assert ext.filler_repeat_ms == 3000
    assert ext.filler_phrases == ()


def test_filler_settings_are_parsed(tmp_path, monkeypatch):
    monkeypatch.setenv("ARI_USER", "phonesys")
    monkeypatch.setenv("ARI_PASSWORD", "secret")
    monkeypatch.setenv("HERMES_PHONEBOT_API_KEY", "hkey")
    cfg = load_config(_write(tmp_path, FILLER_CONFIG_YAML))
    ext = cfg.extensions["7000"]
    assert ext.filler_delay_ms == 800
    assert ext.filler_repeat_ms == 2500
    assert ext.filler_phrases == (
        ("Let me think.", "One moment."),
        ("Still working.",),
    )


def test_filler_phrases_are_hashable(tmp_path, monkeypatch):
    """ExtensionConfig is a frozen dataclass; lists would make it unhashable
    and break any use as a dict key or in a set."""
    monkeypatch.setenv("ARI_USER", "phonesys")
    monkeypatch.setenv("ARI_PASSWORD", "secret")
    monkeypatch.setenv("HERMES_PHONEBOT_API_KEY", "hkey")
    cfg = load_config(_write(tmp_path, FILLER_HASHABLE_YAML))
    hash(cfg.extensions["7000"])


IDLE_HANGUP_YAML = CONFIG_YAML + """
  "7001":
    mode: streaming
    profile: phonebot
    hermes_base_url: http://127.0.0.1:8642
    hermes_api_key_env: HERMES_PHONEBOT_API_KEY
    greeting: "Hi, this is Hermes."
    voice_id: voice-abc
    idle_hangup_seconds: 10
"""


def test_idle_hangup_defaults_to_disabled(tmp_path, monkeypatch):
    """An extension that does not opt in must be untouched -- including 7000,
    which ends calls on its own silence budget and must not grow a second,
    competing mechanism."""
    monkeypatch.setenv("ARI_USER", "phonesys")
    monkeypatch.setenv("ARI_PASSWORD", "secret")
    monkeypatch.setenv("HERMES_PHONEBOT_API_KEY", "hkey")
    cfg = load_config(_write(tmp_path, CONFIG_YAML))
    assert cfg.extensions["7000"].idle_hangup_seconds == 0


def test_idle_hangup_is_parsed(tmp_path, monkeypatch):
    monkeypatch.setenv("ARI_USER", "phonesys")
    monkeypatch.setenv("ARI_PASSWORD", "secret")
    monkeypatch.setenv("HERMES_PHONEBOT_API_KEY", "hkey")
    cfg = load_config(_write(tmp_path, IDLE_HANGUP_YAML))
    assert cfg.extensions["7001"].idle_hangup_seconds == 10
    assert cfg.extensions["7000"].idle_hangup_seconds == 0


NEGATIVE_IDLE_HANGUP_YAML = CONFIG_YAML + """
  "7001":
    mode: streaming
    profile: phonebot
    hermes_base_url: http://127.0.0.1:8642
    hermes_api_key_env: HERMES_PHONEBOT_API_KEY
    greeting: "Hi, this is Hermes."
    voice_id: voice-abc
    idle_hangup_seconds: -10
"""


def test_a_negative_idle_hangup_is_read_as_disabled(tmp_path, monkeypatch):
    """A typo'd minus sign must not hang up on every caller.

    The streaming loop tests `loop.time() - idle_since >= idle_hangup_seconds`,
    which a negative value satisfies on the first pass -- so the call ends the
    instant the greeting stops playing, before the caller can say a word.
    Disabling degrades to the old ten-minute hold; it does not kill the line.
    """
    monkeypatch.setenv("ARI_USER", "phonesys")
    monkeypatch.setenv("ARI_PASSWORD", "secret")
    monkeypatch.setenv("HERMES_PHONEBOT_API_KEY", "hkey")
    cfg = load_config(_write(tmp_path, NEGATIVE_IDLE_HANGUP_YAML))
    assert cfg.extensions["7001"].idle_hangup_seconds == 0


def test_shipped_config_gives_7001_an_idle_hangup_and_7000_none(monkeypatch):
    """The behaviour is only live if the SHIPPED config.yaml opts 7001 in --
    a correct default of 0 everywhere would silently ship the old ten-minute
    hold. 7000 must stay at 0 for the reason above."""
    monkeypatch.setenv("ARI_USER", "phonesys")
    monkeypatch.setenv("ARI_PASSWORD", "secret")
    monkeypatch.setenv("HERMES_PHONEBOT_API_KEY", "hkey")
    # Every configured protected profile needs test-only credentials; do not
    # depend on a developer's live .env to exercise the shipped YAML.
    monkeypatch.setenv("JOHN_YODER_PIN", "123456")
    monkeypatch.setenv("HERMES_JYODER_API_KEY", "john-test-key")
    monkeypatch.setenv("HERMES_ATLAS_API_KEY", "atlas-test-key")
    monkeypatch.setenv("ATLAS_PIN", "24681357")
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    assert cfg.extensions["7001"].idle_hangup_seconds == 10
    assert cfg.extensions["7000"].idle_hangup_seconds == 0


def test_shipped_atlas_extension_is_private_and_directory_listed(monkeypatch):
    monkeypatch.setenv("ARI_USER", "phonesys")
    monkeypatch.setenv("ARI_PASSWORD", "secret")
    monkeypatch.setenv("HERMES_PHONEBOT_API_KEY", "phone-key")
    monkeypatch.setenv("JOHN_YODER_PIN", "123456")
    monkeypatch.setenv("HERMES_JYODER_API_KEY", "john-test-key")
    monkeypatch.setenv("HERMES_ATLAS_API_KEY", "atlas-key")
    monkeypatch.setenv("ATLAS_PIN", "24681357")

    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    atlas = cfg.extensions["7003"]

    assert atlas.profile == "atlas"
    assert atlas.hermes_base_url == "http://127.0.0.1:8644"
    assert atlas.hermes_api_key == "atlas-key"
    assert atlas.access_pin == "24681357"
    assert atlas.directory_listing
    assert atlas.full_name == "Atlas Assistant"
