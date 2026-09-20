#!/usr/bin/env python3
"""Prepare one optional restricted native worker; never start/restart services."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import subprocess
import tempfile

import yaml


# Reviewed against gateway.config.Platform and plugins/platforms in Hermes
# c661785f872b5647fbac7c138d965180783bd9af. Includes bundled dynamic members.
NON_API_PLATFORMS = (
    'local', 'telegram', 'discord', 'whatsapp', 'whatsapp_cloud', 'slack',
    'signal', 'mattermost', 'matrix', 'homeassistant', 'email', 'sms',
    'dingtalk', 'webhook', 'msgraph_webhook', 'feishu', 'wecom',
    'wecom_callback', 'weixin', 'bluebubbles', 'qqbot', 'yuanbao', 'relay',
    'ntfy', 'raft', 'simplex', 'irc', 'google_chat', 'a2a', 'photon',
    'buzz', 'line', 'teams',
)

POLICY = {
    **{f"platforms.{name}.enabled": False for name in NON_API_PLATFORMS},
    "platforms.api_server.enabled": True,
    "platform_toolsets.api_server": ["web", "no_mcp"],
    "agent.max_turns": 8,
    "memory.memory_enabled": False,
    "memory.user_profile_enabled": False,
    "approvals.mode": "manual",
    "approvals.cron_mode": "deny",
    "approvals.single_query_mode": "deny",
    "approvals.unattended_mode": "deny",
    "gateway.multiplex_profiles": False,
    "mcp_servers": {},
    "plugins": {},
    "context_engine": None,
}


class SetupError(Exception):
    """Messages must never contain file contents or subprocess output."""


def read_env(path):
    text = path.read_text() if path.exists() else ""
    values = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([A-Z_][A-Z0-9_]*)=(.*)", line)
        if not match or match[1] in values:
            raise SetupError("Environment files require unique NAME=value assignments")
        try:
            parts = shlex.split(match[2], comments=True)
        except ValueError:
            raise SetupError("Invalid environment value quoting") from None
        if len(parts) > 1:
            raise SetupError("Environment values with spaces must be quoted")
        values[match[1]] = parts[0] if parts else ""
    return text, values


def merged_env(text, updates):
    lines = [line for line in text.splitlines() if line.split("=", 1)[0] not in updates]
    return "\n".join(lines + [f"{key}={value}" for key, value in updates.items()]) + "\n"


def private_write(path, text):
    fd, temporary = tempfile.mkstemp(prefix=".atlas-worker-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_config(path):
    try:
        config = yaml.safe_load(path.read_text())
    except (yaml.YAMLError, UnicodeError):
        raise SetupError("Invalid worker config.yaml") from None
    if not isinstance(config, dict):
        raise SetupError("Worker config.yaml must be a mapping")
    return config


def unit_text(profile, executable, port):
    return f'''[Unit]
Description=Restricted native Hermes worker ({profile.name})
After=network.target

[Service]
Type=simple
WorkingDirectory={profile}
ExecStartPre=/usr/bin/test ! -e /etc/hermes
ExecStart=/usr/bin/env -i "HOME={profile.parents[2]}" "PATH=/usr/local/bin:/usr/bin:/bin" "HERMES_HOME={profile}" "HERMES_SAFE_MODE=1" "HERMES_MANAGED_DIR=/etc/hermes" "{executable}" gateway run --external-supervisor
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=default.target
'''


def check_path(path, *, directory=False, private=True, optional=False):
    if not path.is_absolute() or '..' in path.parts or not re.fullmatch(r"[/A-Za-z0-9_. -]+", str(path)):
        raise SetupError("Paths must be absolute, without traversal or shell/systemd metacharacters")
    for component in reversed((path, *path.parents)):
        if component.is_symlink():
            raise SetupError("Symlink paths are not allowed")
        if component.exists():
            info = component.lstat()
            if info.st_uid not in (0, os.getuid()):
                raise SetupError("Path ancestor has an unexpected owner")
            # A root-owned sticky /tmp is safe above our private temporary HOME.
            if info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
                raise SetupError("Group/world writable path ancestor refused")
    if not path.exists():
        if optional and path.parent.is_dir():
            return
        raise SetupError("Required path does not exist; pre-create the explicit worker and directories")
    info = path.lstat()
    if (directory and not stat.S_ISDIR(info.st_mode)) or (not directory and not stat.S_ISREG(info.st_mode)):
        raise SetupError("Expected a regular file or directory")
    if not directory and info.st_nlink != 1:
        raise SetupError("Hard-linked files are not allowed")
    if private and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise SetupError("Private targets must be owned by the current user, directories 0700/files 0600")


def preflight(args):
    profile = args.profile_dir
    if any((profile / name).exists() or (profile / name).is_symlink()
           for name in ('gateway.json', '.op.env')):
        raise SetupError("Legacy gateway or external-secret env overlays are not supported")
    if (not re.fullmatch(r"[a-z][a-z0-9-]{0,39}-(?:work|worker)", profile.name)
            or profile.parent != Path.home() / '.hermes/profiles'):
        raise SetupError("Use an explicit pre-created $HOME/.hermes/profiles/<name>-work or <name>-worker, never a foreground profile")
    if not 1024 <= args.port <= 65535 or args.port in (8642, 8643, 8644, 9010):
        raise SetupError("Worker port must be 1024..65535, excluding foreground 8642..8644 and bridge 9010")
    for directory in (profile, args.unit_dir, args.bridge_env.parent):
        check_path(directory, directory=True)
    for path in (profile / 'config.yaml', profile / '.env'):
        check_path(path)
    check_path(args.bridge_env, optional=True)
    check_path(args.hermes_executable, private=False)
    if not os.access(args.hermes_executable, os.X_OK):
        raise SetupError("Hermes executable must be executable")
    targets = (profile / 'config.yaml', profile / '.env', args.hermes_executable,
               args.unit_dir / f'hermes-{profile.name}.service', args.bridge_env)
    if len(set(targets)) != len(targets) or args.bridge_env.is_relative_to(profile):
        raise SetupError("Worker and bridge targets must be distinct; bridge env must be outside the worker")
    unit = targets[3]
    check_path(unit, optional=True)
    if unit.exists() and unit.read_text() != unit_text(profile, args.hermes_executable, args.port):
        raise SetupError("Existing unit differs; review and archive only this worker unit before changing it")


def validate_model(config, env):
    model = config.get('model')
    if (not isinstance(model, dict) or not isinstance(model.get('default'), str)
            or not model['default'].strip() or not isinstance(model.get('provider'), str)
            or not model['provider'].strip()):
        raise SetupError("Explicit model.default and model.provider must already be configured in the worker")
    keys = {
        'nous': 'NOUS_API_KEY', 'nous-portal': 'NOUS_API_KEY', 'nousresearch': 'NOUS_API_KEY',
        'openai': 'OPENAI_API_KEY', 'anthropic': 'ANTHROPIC_API_KEY',
        'openrouter': 'OPENROUTER_API_KEY', 'deepseek': 'DEEPSEEK_API_KEY',
        'groq': 'GROQ_API_KEY', 'xai': 'XAI_API_KEY',
    }
    required = keys.get(model['provider'])
    if model['provider'] == 'custom':
        # Deliberately narrow: preserve the configured provider/model and require
        # its own worker key, never infer credentials from a model name or host.
        if (model.get('base_url') != 'https://inference-api.nousresearch.com/v1'
                or model.get('api_key') != '${NOUS_API_KEY}'):
            raise SetupError("Custom worker requires the exact Nous inference endpoint and model.api_key ${NOUS_API_KEY} reference")
        required = 'NOUS_API_KEY'
    if not required:
        raise SetupError("Unsupported model credential preflight; this helper supports explicit API-key providers only, not auto/OAuth/local fallback")
    if not env.get(required, '').strip() or env[required].strip().startswith('${'):
        raise SetupError(f"Missing {required} in explicit worker .env; provision selected model credentials privately first")


def validate_isolation(config, env):
    gateway = config.get('gateway', {})
    # gateway.<platform> is merged AFTER top-level platforms by current Hermes.
    # Refuse ambiguous overlays instead of trusting the raw top-level readback.
    if not isinstance(gateway, dict) or set(gateway) - {'platforms', 'multiplex_profiles'}:
        raise SetupError("Worker gateway overlays are not supported; use a dedicated API-only profile")
    if config.get('secrets'):
        raise SetupError("External secret sources are not supported for this restricted worker")
    for section in (config, config.get('gateway', {})):
        platforms = section.get('platforms', {}) if isinstance(section, dict) else {}
        if not isinstance(platforms, dict):
            raise SetupError("Worker platforms must be a mapping")
        if set(platforms) - {*NON_API_PLATFORMS, 'api_server'}:
            raise SetupError("Unreviewed worker platform; isolation cannot be assured")
        if any(name != 'api_server' and (not isinstance(value, dict) or value.get('enabled') is not False)
               for name, value in platforms.items()):
            raise SetupError("Worker has other platforms; use a dedicated API-only profile")
    # Positive allowlist, not an inevitably incomplete platform-prefix blacklist.
    allowed = {
        'NOUS_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'OPENROUTER_API_KEY',
        'DEEPSEEK_API_KEY', 'GROQ_API_KEY', 'XAI_API_KEY',
        'FIRECRAWL_API_KEY', 'FIRECRAWL_API_URL', 'TAVILY_API_KEY', 'BRAVE_API_KEY',
        'API_SERVER_KEY', 'API_SERVER_ENABLED', 'API_SERVER_HOST', 'API_SERVER_PORT',
        'GATEWAY_MULTIPLEX_PROFILES', 'HERMES_SAFE_MODE',
    }
    if set(env) - allowed:
        raise SetupError("Worker .env contains unreviewed settings; only explicit provider/web/API keys are allowed")


def prepare(args):
    preflight(args)
    profile = args.profile_dir
    config_path = profile / "config.yaml"
    config = load_config(config_path)
    env_path = profile / ".env"
    env_text, env = read_env(env_path)
    bridge_text, _ = read_env(args.bridge_env)
    validate_model(config, env)
    validate_isolation(config, env)
    key = env.get("API_SERVER_KEY") or secrets.token_hex(32)
    if not re.fullmatch(r'[A-Za-z0-9_-]{43,256}', key):
        raise SetupError("Existing API_SERVER_KEY must be a strong URL-safe token; rotate it privately before setup")
    controls = {
        "API_SERVER_ENABLED": "true", "API_SERVER_HOST": "127.0.0.1",
        "API_SERVER_PORT": str(args.port), "HERMES_SAFE_MODE": "1",
        "GATEWAY_MULTIPLEX_PROFILES": "false",
    }
    child_env = {"HOME": str(Path.home()), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "HERMES_HOME": str(profile)}
    # Use Hermes' own writer; never hand-edit an existing user's configuration.
    for setting, value in POLICY.items():
        # The reviewed writer guards existing top-level mappings before parsing
        # JSON. Only these deliberate whole-section policy resets may replace one.
        replace_section = ['--force'] if setting in {'mcp_servers', 'plugins', 'context_engine'} else []
        result = subprocess.run([str(args.hermes_executable), "-p", profile.name,
                                 "config", "set", setting, value if isinstance(value, str) else json.dumps(value),
                                 *replace_section],
                                env=child_env, cwd=profile, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=60)
        if result.returncode:
            raise SetupError("Hermes config set failed; partial policy possible, service not started")
    observed = load_config(config_path)
    validate_isolation(observed, env)
    for setting, expected in POLICY.items():
        value = observed
        for part in setting.split('.'):
            value = value.get(part, object()) if isinstance(value, dict) else object()
        if type(value) is not type(expected) or value != expected:
            raise SetupError("Worker policy readback failed; no token distributed or service started")
    if observed.get("model") != config.get("model"):
        raise SetupError("Model configuration changed unexpectedly; service not started")
    # Recheck all targets after the CLI writer, before distributing credentials.
    preflight(args)
    private_write(env_path, merged_env(env_text, {**controls, "API_SERVER_KEY": key}))
    private_write(args.bridge_env, merged_env(bridge_text, {"HERMES_ATLAS_WORK_API_KEY": key}))
    private_write(args.unit_dir / f"hermes-{profile.name}.service",
                  unit_text(profile, args.hermes_executable, args.port))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("profile-dir", "hermes-executable", "unit-dir", "bridge-env"):
        parser.add_argument("--" + argument, type=Path, required=True)
    parser.add_argument("--port", type=int, default=8645)
    args = parser.parse_args()
    try:
        prepare(args)
    except (SetupError, OSError, subprocess.SubprocessError) as exc:
        message = str(exc) if isinstance(exc, SetupError) else "Filesystem or CLI failure; no service started"
        parser.exit(1, "setup-atlas-worker: " + message + "\n")
    print("Restricted worker prepared; token synchronized privately. No service started or restarted.")


if __name__ == "__main__":
    main()
