"""Cancellation regressions against a real, isolated loopback HTTP gateway."""
import asyncio
import json
from typing import Any

import pytest

from phonesys.native_work import WorkCall


@pytest.fixture
async def gateway(tmp_path):
    state = {"status": "running", "fail_get": 0, "fail_deny": False,
             "fail_stop": False, "stuck_stop": False, "requests": [],
             "stall": "", "release": asyncio.Event()}

    async def serve(reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            lines = header.decode().split("\r\n")
            method, path, _ = lines[0].split()
            headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            length = int(headers.get("Content-Length", "0"))
            body = json.loads(await reader.readexactly(length)) if length else {}
            state["requests"].append((method, path, body))
            if state["stall"] == (method, path):
                await state["release"].wait()
            code = 200
            response: dict[str, Any] = {"run_id": "run_known", "status": state["status"]}
            if path == "/v1/toolsets":
                response = {"data": [{"enabled": True, "tools": ["web_search"]}]}
            elif path == "/v1/runs" and method == "POST":
                response = {"run_id": "run_known", "status": "started"}
            elif method == "GET" and state["fail_get"]:
                state["fail_get"] -= 1
                code = 503
            elif path.endswith("/approval"):
                code = 503 if state["fail_deny"] else 200
            elif path.endswith("/stop"):
                code = 503 if state["fail_stop"] else 200
                if code == 200:
                    state["status"] = "stopping" if state["stuck_stop"] else "cancelled"
                # Stop acceptance is deliberately not terminal-state evidence.
                response = {"accepted": code == 200}
            if state["status"] == "waiting_for_approval":
                response["approval"] = {"request_id": "approval_known", "command": "secret"}
            data = json.dumps(response).encode()
            writer.write(f"HTTP/1.1 {code} Response\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    config = tmp_path / "worker.yaml"
    config.write_text("platform_toolsets:\n  api_server: [web, no_mcp]\nagent:\n  max_turns: 8\n")
    work = WorkCall("cancel-test", f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                    "test-only", tmp_path / "private", config)
    await work.start()
    await work.command({"action": "dispatch", "key": "a", "subject": "Research", "scope": "read_only"})
    state["requests"].clear()
    try:
        yield work, state
    finally:
        state["stall"] = ""
        state["release"].set()
        if not work.runs.client.is_closed:
            await work.close()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("trigger", ["cancel", "deadline", "approval", "approval_hangup"])
async def test_required_stop_survives_failed_control_request(gateway, trigger):
    work, state = gateway
    if trigger in {"cancel", "deadline"}:
        if trigger == "deadline":
            work.jobs["a"]["deadline"] = 0
        state["fail_get"] = 1
    else:
        state.update(status="waiting_for_approval", fail_deny=True)
    if trigger == "approval_hangup":
        await work.close()
    else:
        result = await work.command({"action": "cancel" if trigger == "cancel" else "status", "key": "a"})
        assert result["status"] == "cancelled"
    assert sum(path.endswith("/stop") for _, path, _ in state["requests"]) == 1
    assert state["status"] == "cancelled"
    if trigger.startswith("approval"):
        denial = next(body for _, path, body in state["requests"] if path.endswith("/approval"))
        assert denial == {"choice": "deny", "request_id": "approval_known"}


async def test_hangup_failed_get_still_stops_known_run(gateway):
    work, state = gateway
    state["fail_get"] = 1
    await asyncio.wait_for(work.close(), 1)
    assert state["requests"] == [
        ("GET", "/v1/runs/run_known", {}),
        ("POST", "/v1/runs/run_known/stop", {}),
        ("GET", "/v1/runs/run_known", {}),
    ]
    assert state["status"] == "cancelled"
    assert not work.record_path.exists()
    assert work.runs.client.is_closed


@pytest.mark.parametrize("trigger", ["cancel", "deadline", "hangup"])
@pytest.mark.parametrize("failure", ["stop", "confirmation", "stopping"])
async def test_unconfirmed_termination_retains_reservation(gateway, trigger, failure):
    work, state = gateway
    original = dict(work.jobs["a"])
    if failure == "stop":
        state["fail_stop"] = True
    elif failure == "confirmation":
        state["fail_get"] = 10
    else:
        state["stuck_stop"] = True
    if trigger == "deadline":
        work.jobs["a"]["deadline"] = 0
    if trigger == "hangup":
        await work.close()
    else:
        result = await work.command({"action": "cancel" if trigger == "cancel" else "status", "key": "a"})
        assert result["status"] not in {"completed", "failed", "cancelled", "interrupted"}
        assert result["termination_unconfirmed"] is True
        assert "unconfirmed" in result["detail"]
    assert sum(path.endswith("/stop") for _, path, _ in state["requests"]) == 1
    saved = json.loads(work.record_path.read_text())["jobs"]["a"]
    assert saved["termination_unconfirmed"] is True
    for field in ("run_id", "fingerprint", "idempotency_key", "session_id"):
        assert saved[field] == original[field]


@pytest.mark.parametrize("failure", ["fail_stop", "stuck_stop"])
async def test_later_terminal_observation_releases_unresolved_reservation(gateway, failure):
    work, state = gateway
    state[failure] = True
    result = await work.command({"action": "cancel", "key": "a"})
    assert result["termination_unconfirmed"] is True
    state["status"] = "cancelled"
    result = await work.command({"action": "status", "key": "a"})
    assert result["status"] == "cancelled"
    assert not result.get("termination_unconfirmed")
    assert not json.loads(work.record_path.read_text())["jobs"]["a"].get("termination_unconfirmed")
    await work.close()
    assert not work.record_path.exists()


@pytest.mark.parametrize("phase", ["get", "deny", "stop"])
async def test_control_stalls_are_bounded_without_skipping_stop(gateway, monkeypatch, phase):
    work, state = gateway
    # Scale only the production control timeout; the real HTTP request still stalls.
    monkeypatch.setattr(work, "_CONTROL_TIMEOUT", 0.05, raising=False)
    state["stall"] = {
        "get": ("GET", "/v1/runs/run_known"),
        "deny": ("POST", "/v1/runs/run_known/approval"),
        "stop": ("POST", "/v1/runs/run_known/stop"),
    }[phase]
    if phase == "deny":
        state["status"] = "waiting_for_approval"
    result = await asyncio.wait_for(work.command({"action": "cancel", "key": "a"}), 1)
    assert sum(path.endswith("/stop") for _, path, _ in state["requests"]) == 1
    if phase == "deny":
        assert result["status"] == "cancelled"
    else:
        assert result["termination_unconfirmed"] is True
        assert work.record_path.exists()


async def test_watchdog_deadline_failed_get_still_stops(gateway):
    work, state = gateway
    work.jobs["a"]["deadline"] = 0
    state["fail_get"] = 1
    async with asyncio.timeout(3):
        while state["status"] != "cancelled":
            await asyncio.sleep(0.01)
    async with work.lock:
        assert sum(path.endswith("/stop") for _, path, _ in state["requests"]) == 1
        assert not work.jobs["a"].get("termination_unconfirmed")
    await work.close()
    assert not work.record_path.exists()


async def test_observation_failure_before_deadline_is_not_cancellation(gateway):
    from phonesys.native_work import WorkError

    work, state = gateway
    state["fail_get"] = 1
    with pytest.raises(WorkError, match="no terminal confirmation"):
        await work.command({"action": "status", "key": "a"})
    assert state["requests"] == [("GET", "/v1/runs/run_known", {})]
    assert state["status"] == "running"
    assert work.record_path.exists()
