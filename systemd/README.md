# Systemd user units

Do not copy historical units from an older checkout. All four current units are
rendered with absolute, systemd-escaped paths by `scripts/setup.py generate`:
`hermes-phonebot.service`, `hermes-jyoder.service`, `hermes-atlas.service`, and
`phonesys-bridge.service`. See [SETUP](../docs/SETUP.md) for manual activation.

Each gateway gets its own HERMES_HOME, EnvironmentFile, loopback API port and key.
The bridge gets only bridge credentials. API behavior env names match Hermes
0.21.3 gateway/config_env.py. Runtime policies are set with `hermes config set`.
Units use UMask=0077 and restart-on-failure, but are NOT sandboxes: reviewed Hermes
tools run with the Unix user's access. User services do not require root; WSL
must run systemd. No installer invokes daemon-reload, enable, start or Docker.
