"""Offline native-work tests: a real loopback HTTP server, never a provider."""
import asyncio
import importlib.util
import json
from pathlib import Path

import pytest


class RequestLog(list):
    drop_submit = False
    stuck_stop = False
    unsafe_tools = False


@pytest.fixture
async def native():
    requests = RequestLog()
    statuses = {}
    async def serve(reader, writer):
        header = await reader.readuntil(b"\r\n\r\n")
        lines = header.decode().split("\r\n")
        method, path, _ = lines[0].split()
        headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
        length = int(headers.get("Content-Length", "0"))
        body = json.loads(await reader.readexactly(length)) if length else {}
        requests.append((method, path, headers, body))
        if path == "/api/sessions" and method == "POST":
            response = {"session": {"id": body["id"]}}
        elif path == "/v1/runs" and method == "POST":
            run_id = "run_" + headers["Idempotency-Key"][-16:]
            statuses.setdefault(run_id, {"run_id": run_id, "status": "running", "output": ""})
            if requests.drop_submit:
                requests.drop_submit = False
                writer.close()
                await writer.wait_closed()
                return
            response = {"run_id": run_id, "status": "started"}
        elif path == "/v1/toolsets":
            response = {"data": [{"name": "web", "enabled": True, "tools": (["terminal"] if requests.unsafe_tools else ["web_search", "web_extract"])}]}
        else:
            run_id = path.split("/")[3]
            if path.endswith("/stop"):
                statuses[run_id]["status"] = "stopping" if requests.stuck_stop else "cancelled"
            response = statuses[run_id]
        data = json.dumps(response).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(data)).encode() + b"\r\nConnection: close\r\n\r\n" + data)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", requests, statuses
    server.close()
    await server.wait_closed()


def worker_config(tmp_path):
    config = tmp_path / "worker.yaml"
    config.write_text("platform_toolsets:\n  api_server: [web, no_mcp]\nagent:\n  max_turns: 8\n")
    return config


async def test_admission_association_idempotency_and_two_slots(native, tmp_path):
    from phonesys import native_work
    assert hasattr(native_work, "WorkCall"), "authenticated call association missing"
    url, requests, statuses = native
    work = native_work.WorkCall("call-authenticated", url, "test-only", tmp_path / "private", worker_config(tmp_path))
    await work.start()
    try:
        a = await work.command({"action": "dispatch", "key": "a", "subject": "Compare A and B", "scope": "read_only"})
        again = await work.command({"action": "dispatch", "key": "a", "subject": "Compare A and B", "scope": "read_only"})
        assert a["run_id"] == again["run_id"]
        assert len([r for r in requests if r[:2] == ("POST", "/v1/runs")]) == 1
        with pytest.raises(native_work.WorkError, match="different subject"):
            await work.command({"action": "dispatch", "key": "a", "subject": "Changed", "scope": "read_only"})
        await work.command({"action": "dispatch", "key": "b", "subject": "Research another topic", "scope": "read_only"})
        with pytest.raises(native_work.WorkError, match="two"):
            await work.command({"action": "dispatch", "key": "c", "subject": "Third", "scope": "read_only"})
        with pytest.raises(native_work.WorkError, match="this call"):
            await work.command({"action": "result", "key": "someone-elses-job"})
        with pytest.raises(native_work.WorkError, match="request"):
            await work.command({"action": "cancel", "key": "a", "run_id": "run_foreign"})
        assert (await work.command({"action": "cancel", "key": "a"}))["status"] == "cancelled"
        await work.command({"action": "dispatch", "key": "c", "subject": "Third", "scope": "read_only"})
    finally:
        await work.close()
    assert all(s["status"] == "cancelled" for s in statuses.values())
    assert not work.socket_path.exists()


async def test_worker_policy_fails_closed(native, tmp_path):
    from phonesys.native_work import WorkCall, WorkError
    url, requests, _ = native
    policy = worker_config(tmp_path)
    policy.write_text("platform_toolsets:\n  api_server: [terminal]\nagent:\n  max_turns: unlimited\n")
    work = WorkCall("call-auth", url, "test-only", tmp_path / "private", policy)
    with pytest.raises(WorkError, match="worker policy"):
        await work.start()
    assert not requests
    await work.runs.client.aclose()


async def test_approval_denied_and_deadline_requests_stop(native, tmp_path):
    from phonesys.native_work import WorkCall
    url, requests, statuses = native
    work = WorkCall("call-auth", url, "test-only", tmp_path / "private", worker_config(tmp_path))
    await work.start()
    try:
        created = await work.command({"action": "dispatch", "key": "a", "subject": "Research A", "scope": "read_only"})
        run_id = created["run_id"]
        statuses[run_id].update(status="waiting_for_approval", approval={"request_id": "approval-a", "command": "sensitive"})
        result = await work.command({"action": "status", "key": "a"})
        assert result["needs_approval"] is True
        assert "approval" not in result
        assert (await work.command({"action": "status", "key": "a"}))["needs_approval"] is True
        assert "approval" not in await work.command({"action": "cancel", "key": "a"})
        assert next(r for r in requests if r[1].endswith("/approval"))[3] == {"choice": "deny", "request_id": "approval-a"}
        statuses[run_id]["status"] = "running"
        work.jobs["a"]["deadline"] = 0
        assert (await work.command({"action": "status", "key": "a"}))["status"] == "cancelled"
    finally:
        await work.close()


async def test_private_socket_and_standalone_cli_context(native, tmp_path):
    import os
    import sys
    from phonesys.native_work import WorkCall
    url, requests, _ = native
    work = WorkCall("call-auth", url, "test-only", tmp_path / "private", worker_config(tmp_path))
    await work.start()
    try:
        assert work.socket_path.exists(), "foreground helper socket missing"
        assert work.socket_path.stat().st_mode & 0o777 == 0o600
        script = Path(__file__).resolve().parents[2] / "skills/atlas-native-work/scripts/atlas-work.py"
        assert script.exists()
        env = {**os.environ, "HERMES_SESSION_PLATFORM": "api_server", "HERMES_SESSION_CHAT_ID": "call-auth", "HERMES_SESSION_ID": "deliberately-not-public-id"}
        process = await asyncio.create_subprocess_exec(sys.executable, str(script), "--socket-dir", str(tmp_path / "private"), "dispatch", "a", "Compare supplied options", env=env, stdout=asyncio.subprocess.PIPE)
        stdout, _ = await process.communicate()
        assert process.returncode == 0, stdout
        assert json.loads(stdout)["status"] == "running"
        env["HERMES_SESSION_CHAT_ID"] = "phonework-worker"
        process = await asyncio.create_subprocess_exec(sys.executable, str(script), "--socket-dir", str(tmp_path / "private"), "list", env=env, stdout=asyncio.subprocess.PIPE)
        stdout, _ = await process.communicate()
        assert process.returncode == 1
        assert "active authenticated" in stdout.decode()
    finally:
        await work.close()


async def test_hermes_session_lifetime_owns_work_not_reply_stream(native, tmp_path):
    from types import SimpleNamespace
    from phonesys.hermes import HermesClient
    url, requests, statuses = native
    settings = SimpleNamespace(base_url=url, directory=tmp_path / "private", worker_config=worker_config(tmp_path))
    client = HermesClient(url, "test-only", native_work=settings)
    assert client._work is None
    await client.create_session("call-auth")
    assert client._work.active
    created = await client._work.command({"action": "dispatch", "key": "a", "subject": "A", "scope": "read_only"})
    socket_path = client._work.socket_path
    assert requests[0][1] == "/api/sessions"
    assert requests[0][3] == {"id": "call-auth"}  # no prompt or transcript mutation
    await client.aclose()
    assert statuses[created["run_id"]]["status"] == "cancelled"
    assert not socket_path.exists()


async def test_global_slots_across_authenticated_calls(native, tmp_path):
    from phonesys.native_work import WorkCall, WorkError
    url, requests, _ = native
    policy = worker_config(tmp_path)
    first = WorkCall("call-first", url, "test-only", tmp_path / "private", policy)
    second = WorkCall("call-second", url, "test-only", tmp_path / "private", policy)
    await first.start()
    await second.start()
    try:
        for key in ("a", "b"):
            await first.command({"action": "dispatch", "key": key, "subject": key, "scope": "read_only"})
        with pytest.raises(WorkError, match="two"):
            await second.command({"action": "dispatch", "key": "c", "subject": "c", "scope": "read_only"})
        with pytest.raises(WorkError, match="this call"):
            await second.command({"action": "cancel", "key": "a"})
    finally:
        await first.close()
        await second.close()


@pytest.mark.parametrize("payload", [
    {"action": []}, {"action": "status", "key": []},
    {"action": "dispatch", "key": "bad", "subject": "do it", "scope": "write"},
    {"action": "dispatch", "key": "../other", "subject": "x", "scope": "read_only"},
    {"action": "dispatch", "key": "a", "subject": "x" * 4001, "scope": "read_only"},
    {"action": "status", "key": "a", "run_id": "run_foreign"},
])
async def test_bad_helper_input_never_submits(native, tmp_path, payload):
    from phonesys.native_work import WorkCall, WorkError
    url, requests, _ = native
    work = WorkCall("call-auth", url, "test-only", tmp_path / "private", worker_config(tmp_path))
    await work.start()
    try:
        with pytest.raises(WorkError):
            await work.command(payload)
        assert not [r for r in requests if r[:2] == ("POST", "/v1/runs")]
    finally:
        await work.close()


@pytest.mark.parametrize("expired", [False, True])
async def test_uncertain_submission_replay_is_explicit_and_expired_replay_denied(native, tmp_path, expired):
    from phonesys.native_work import WorkCall, WorkError
    url, requests, statuses = native
    work = WorkCall("call-auth", url, "test-only", tmp_path / "private", worker_config(tmp_path))
    await work.start()
    payload = {"action": "dispatch", "key": "a", "subject": "Research A", "scope": "read_only"}
    try:
        requests.drop_submit = True
        with pytest.raises(WorkError, match="uncertain"):
            await work.command(payload)
        assert len(statuses) == 1
        assert (await work.command({"action": "list"}))["jobs"]["a"]["status"] == "acceptance_unknown"
        assert len([r for r in requests if r[:2] == ("POST", "/v1/runs")]) == 1
        assert work.record_path.stat().st_mode & 0o777 == 0o600
        record = work.record_path.read_text()
        assert "Research A" not in record and "test-only" not in record
        if expired:
            work.jobs["a"]["deadline"] = 0
            with pytest.raises(WorkError, match="deadline"):
                await work.command(payload)
        else:
            result = await work.command(payload)
            assert result["run_id"] in statuses
            posts = [r for r in requests if r[:2] == ("POST", "/v1/runs")]
            assert len(posts) == 2 and len(statuses) == 1
            assert posts[0][2]["Idempotency-Key"] == posts[1][2]["Idempotency-Key"]
            assert posts[0][3] == posts[1][3]
    finally:
        await work.close()
    assert work.record_path.exists() == expired  # unknown acceptance never silently released


async def test_cooperative_stop_not_claimed_terminal_and_policy_drift_denied(native, tmp_path):
    from phonesys.native_work import WorkCall, WorkError
    url, requests, statuses = native
    work = WorkCall("call-auth", url, "test-only", tmp_path / "private", worker_config(tmp_path))
    await work.start()
    try:
        await work.command({"action": "dispatch", "key": "a", "subject": "A", "scope": "read_only"})
        requests.stuck_stop = True
        assert (await work.command({"action": "cancel", "key": "a"}))["status"] == "stopping"
        requests.unsafe_tools = True
        with pytest.raises(WorkError, match="non-read-only"):
            await work.command({"action": "dispatch", "key": "b", "subject": "B", "scope": "read_only"})
    finally:
        await work.close()
    assert work.record_path.exists()


async def test_helper_disconnect_preserves_work_and_watchdog_observes_deadline(native, tmp_path):
    from phonesys.native_work import WorkCall
    url, requests, statuses = native
    work = WorkCall("call-auth", url, "test-only", tmp_path / "private", worker_config(tmp_path))
    await work.start()
    try:
        reader, writer = await asyncio.open_unix_connection(work.socket_path)
        writer.write(json.dumps({"action": "dispatch", "key": "a", "subject": "A", "scope": "read_only"}).encode() + b"\n")
        await writer.drain()
        writer.close()  # same helper-disconnect effect as foreground cancellation/barge-in
        await writer.wait_closed()
        async with asyncio.timeout(2):
            while not work.jobs.get("a", {}).get("run_id"):
                await asyncio.sleep(0.01)
        run_id = work.jobs["a"]["run_id"]
        assert statuses[run_id]["status"] == "running"
        assert not any(r[1].endswith("/stop") for r in requests)
        work.jobs["a"]["deadline"] = 0
        async with asyncio.timeout(3):
            while statuses[run_id]["status"] != "cancelled":
                await asyncio.sleep(0.01)
    finally:
        await work.close()


async def test_duplicate_call_cannot_replace_active_socket(native, tmp_path):
    from phonesys.native_work import WorkCall, WorkError
    url, _, _ = native
    policy = worker_config(tmp_path)
    first = WorkCall("call-auth", url, "test-only", tmp_path / "private", policy)
    duplicate = WorkCall("call-auth", url, "test-only", tmp_path / "private", policy)
    await first.start()
    try:
        original = first.socket_path.stat().st_ino
        with pytest.raises(WorkError, match="endpoint"):
            await duplicate.start()
        assert first.socket_path.stat().st_ino == original
    finally:
        await first.close()
        await duplicate.runs.client.aclose()


async def test_native_wire_contract(native):
    assert importlib.util.find_spec("phonesys.native_work") is not None, "native work transport missing"
    from phonesys.native_work import NativeRuns
    url, requests, statuses = native
    async with NativeRuns(url, "test-only") as transport:
        created = await transport.submit("compare supplied options", "phonework-a", "phonework-key")
        run_id = created["run_id"]
        assert (await transport.get(run_id))["status"] == "running"
        await transport.steer(run_id, "focus on costs")
        assert (await transport.stop(run_id))["status"] == "cancelled"
    method, path, headers, body = requests[0]
    assert (method, path) == ("POST", "/v1/runs")
    assert headers["Authorization"] == "Bearer test-only"
    assert headers["Idempotency-Key"] == "phonework-key"
    assert body["session_id"] == "phonework-a"
    assert body["input"] == "compare supplied options"
    assert set(body) == {"input", "session_id", "instructions"}
    assert requests[2][3] == {"message": "focus on costs"}
    assert requests[-1][:2] == ("GET", "/v1/runs/" + run_id)
