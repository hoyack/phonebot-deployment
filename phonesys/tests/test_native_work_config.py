from pathlib import Path

import pytest

from phonesys.config import load_config
from test_config import CONFIG_YAML


def config_file(tmp_path, monkeypatch, *, profile="atlas", mode="streaming", pin=True, worker_url="http://127.0.0.1:8642/p/atlas-work"):
    for key in ("ARI_USER", "ARI_PASSWORD", "HERMES_PHONEBOT_API_KEY", "TEST_PIN"):
        monkeypatch.setenv(key, "test-only")
    text = CONFIG_YAML.replace("profile: phonebot", f"profile: {profile}")
    text += f"    mode: {mode}\n"
    if pin:
        text += "    access_pin_env: TEST_PIN\n"
    text += f"    native_work:\n      base_url: {worker_url}\n      directory: {tmp_path}/private\n      worker_config: {tmp_path}/atlas-work/config.yaml\n"
    path = tmp_path / "phonesys.yaml"
    path.write_text(text)
    return path


async def test_config_is_opt_in_and_streaming_factory_passes_settings(tmp_path, monkeypatch):
    from phonesys import main
    ext = load_config(config_file(tmp_path, monkeypatch)).extensions["7000"]
    assert hasattr(ext, "native_work"), "native work configuration missing"
    assert ext.native_work.directory == tmp_path / "private"
    client = main._streaming_hermes(ext)
    assert client._work_settings == ext.native_work
    assert client._work is None  # no pre-PIN association or HTTP
    await client.aclose()


@pytest.mark.parametrize("overrides", [
    {"profile": "mary"}, {"pin": False}, {"mode": "turn"},
    {"worker_url": "https://elsewhere.invalid/p/atlas-work"},
    {"worker_url": "http://127.0.0.1:8642"},
    {"worker_url": "http://127.0.0.1:8642/p/atlas"},
])
def test_config_rejects_unsafe_native_work(tmp_path, monkeypatch, overrides):
    with pytest.raises(ValueError, match="native_work"):
        load_config(config_file(tmp_path, monkeypatch, **overrides))
