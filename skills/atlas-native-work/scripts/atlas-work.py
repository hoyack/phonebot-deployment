#!/usr/bin/env python3
"""Foreground-only native-work helper. Stdlib only; no credentials or run state."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket-dir", type=Path, help="Override installed nonsecret socket directory")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    dispatch = sub.add_parser("dispatch")
    dispatch.add_argument("key")
    dispatch.add_argument("subject")
    for action in ("status", "result", "cancel"):
        sub.add_parser(action).add_argument("key")
    steer = sub.add_parser("steer")
    steer.add_argument("key")
    steer.add_argument("message")
    args = parser.parse_args()
    try:
        # api_server._prepare_session_chat -> _run_agent -> _bind_api_server_session
        # binds the URL's public id to CHAT_ID. SESSION_ID can rotate; never use it.
        call_id = os.environ.get("HERMES_SESSION_CHAT_ID", "")
        if os.environ.get("HERMES_SESSION_PLATFORM") != "api_server" or not call_id.startswith("call-"):
            raise ValueError("No active authenticated Atlas call context")
        directory = args.socket_dir
        if directory is None:
            directory = Path(json.loads(Path(__file__).with_name("settings.json").read_text())["socket_dir"])
        path = directory / (hashlib.sha256(call_id.encode()).hexdigest()[:16] + ".sock")
        request = {k: v for k, v in vars(args).items() if k != "socket_dir"}
        if args.action == "dispatch":
            request["scope"] = "read_only"
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(45)
            client.connect(str(path))
            client.sendall(json.dumps(request).encode() + b"\n")
            with client.makefile("rb") as stream:
                response = json.loads(stream.readline(131072))
        print(json.dumps(response))
        return 1 if "error" in response else 0
    except (OSError, ValueError, KeyError):
        print(json.dumps({"error": "No active authenticated Atlas work connection; do not retry automatically"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
