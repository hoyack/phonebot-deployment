# Generated deployment templates

`scripts/setup.py generate` is the single renderer for three independent profile
identities, keys, four systemd user units and a separate runtime bridge config.
It reads the checked-in `phonebot/SOUL.md` and `phonesys/config.yaml`; it never
reads existing user profiles or credentials. `phonebot/config.yaml` is a policy
reference only: apply settings using the commands in [SETUP](../docs/SETUP.md).
John and Atlas start with minimal fresh SOUL files, not copied private personas.
Review/customize all three before exposure to callers. No default-profile state,
OAuth, messaging tokens, memories, skills or MCP servers are cloned.

The runtime config changes only John (7002) to `HERMES_JYODER_API_KEY` so fresh
installs have independently revocable gateway credentials. The shipped source
snapshot intentionally retains its original live-equivalent mapping.
