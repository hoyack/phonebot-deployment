"""Dedicated restricted gateway support without broad profile multiplexing."""
import pytest
from phonesys.config import load_config, MissingSecretError
from test_native_work_config import config_file


def worker_config(tmp_path, monkeypatch, url="http://127.0.0.1:8645", secret=True):
    path = config_file(tmp_path, monkeypatch, worker_url=url)
    path.write_text(path.read_text() + "      api_key_env: TEST_WORKER_KEY\n")
    if secret:
        monkeypatch.setenv("TEST_WORKER_KEY", "worker-test-only")
    else:
        monkeypatch.delenv("TEST_WORKER_KEY", raising=False)
    return path


async def test_dedicated_worker_has_separate_key_and_no_foreground_token(tmp_path, monkeypatch):
    from phonesys.main import _streaming_hermes
    ext = load_config(worker_config(tmp_path, monkeypatch)).extensions["7000"]
    assert ext.native_work.api_key == "worker-test-only"
    assert "worker-test-only" not in repr(ext.native_work)
    client = _streaming_hermes(ext)
    try:
        assert client._work_key == "worker-test-only"
        assert client._client.headers["Authorization"] == "Bearer test-only"
    finally:
        await client.aclose()


def test_missing_dedicated_key_fails_closed(tmp_path, monkeypatch):
    with pytest.raises(MissingSecretError):
        load_config(worker_config(tmp_path, monkeypatch, secret=False))


@pytest.mark.parametrize("url", [
    "http://localhost:8645", "http://192.168.0.142:8645",
    "https://elsewhere.invalid", "http://127.0.0.1:8642",
    "http://127.0.0.1:8645/p/atlas", "http://127.0.0.1:8645?key=bad",
    "http://user:password@127.0.0.1:8645", "http://127.0.0.1:8645#bad",
])
def test_dedicated_worker_rejects_nonliteral_loopback_or_ambiguous_target(tmp_path, monkeypatch, url):
    with pytest.raises(ValueError, match="native_work"):
        load_config(worker_config(tmp_path, monkeypatch, url=url))
