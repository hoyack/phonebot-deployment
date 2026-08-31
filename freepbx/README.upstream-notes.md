# FreePBX 17 — local lab PBX

FreePBX 17.0.21 / Asterisk 21.10.2 in Docker, adapted from
[escomputers/freepbx-docker](https://github.com/escomputers/freepbx-docker).

## Quick start

```bash
docker compose up -d

# One-time: run the FreePBX installer against the DB
docker compose exec -w /usr/local/src/freepbx freepbx \
  php install -n --dbuser=freepbxuser \
  --dbpass="$(cat secrets/freepbxuser_password.txt)" --dbhost=db
```

Then open <http://localhost:8080> and create the admin account.

## Ports

| Host | Container | Purpose |
|---|---|---|
| 8080 | 80/tcp | FreePBX admin GUI |
| 8443 | 443/tcp | HTTPS |
| 127.0.0.1:8088 | 8088/tcp | ARI (loopback-only) |
| 5060 | 5060/udp | PJSIP signalling |
| 10000-10099 | same/udp | RTP media (~50 concurrent calls) |

FreePBX defaults RTP to 16384-32767. Publishing 16k UDP ports through Docker
Desktop is not viable, so the range is capped at 100 and must be set to match
under **Settings → Asterisk SIP Settings → RTP Port Range**.

## Deviations from upstream

- **`run.sh` is not used.** It installs host `iptables` DNAT rules for the RTP
  range. Under Docker Desktop the daemon lives in a separate `docker-desktop`
  WSL VM, so there is no `docker0`/`br-*` bridge in this distro's namespace —
  those rules would never see container traffic. RTP is published by Compose
  instead.
- **fail2ban container removed.** Docker Desktop's proxy rewrites packet source
  addresses to the bridge gateway. fail2ban would read that gateway as the
  attacker and ban it, blackholing all SIP traffic including the trunk. Uses the
  `17-nofail2ban` image tag.
- **Subnet is `10.77.0.0/24`, not `172.18.0.0/16`.** On this host `172.18.0.0/16`
  is already allocated to `tools-factory-server_shrubnet`, and it would also
  swallow the WSL2 `eth0` at `172.18.36.168/20`. All of `172.17`–`172.31` is
  consumed by other stacks here.
- **GUI on 8080/8443** rather than 80/443, leaving those free for traefik.

## NAT settings for real calls

These are **already configured** (Settings → Asterisk SIP Settings):

| Setting | Value |
|---|---|
| External Address | `148.66.37.86` — auto-detected at install |
| Local Networks | `10.77.0.0/24`, `192.168.0.0/24`, `172.18.32.0/20` |
| RTP Port Range | `10000-10099` |

The RTP range **must** stay in sync with the published range in
`docker-compose.yml`. FreePBX defaults to `10000-20000`; left at the default,
Asterisk allocates across the full 10k span while only 100 ports are reachable,
so roughly 99% of calls get no audio at all. If you change one, change both.

The External Address is a dynamic residential IP — if it changes, trunk
registration breaks until it's updated.

Use a **registration-based** SIP trunk (Telnyx, Flowroute, VoIP.ms, Skyetel).
The outbound REGISTER holds a NAT pinhole open so inbound INVITEs return through
it — no router port-forwarding needed.

### Where these settings actually live

Not obvious, and easy to get wrong:

- The legacy `sipsettings` table is **not** the source of truth. The module
  explicitly deletes `rtpstart`/`rtpend` from it (`sipsettings/install.php:96`)
  — writes there are silently discarded.
- Real storage is BMO KVStore, table `kvstore_Sipsettings`, with `id='noid'`.
- Scalars can be written as plain rows, but array values like `localnets` are
  serialized by BMO. Set those through the API rather than by hand:

```bash
docker compose exec -T freepbx php -r '
include "/etc/freepbx.conf";
$ss = FreePBX::Sipsettings();
$ss->setConfig("localnets", [["net"=>"10.77.0.0","mask"=>"255.255.255.0"]]);
'
docker compose exec -T freepbx fwconsole reload
```

Always verify against live Asterisk, not the DB or generated files:

```bash
docker compose exec -T freepbx asterisk -rx "rtp show settings"
```

## ARI (Asterisk REST Interface)

Enabled for local call-control development. Base URL `http://127.0.0.1:8088/ari`,
user `phonesys` (credentials in `secrets/ari.txt`).

```bash
curl -s -u "phonesys:$(cut -d= -f2 secrets/ari.txt)" \
  http://127.0.0.1:8088/ari/asterisk/info
```

Two pieces had to line up, and both are easy to get wrong:

- **The HTTP server is off by default.** It is enabled through FreePBX, *not* by
  editing `http_additional.conf` — that file is auto-generated and hand edits are
  lost on the next `fwconsole reload`:
  ```bash
  fwconsole setting HTTPENABLED 1
  fwconsole setting HTTPBINDADDRESS 0.0.0.0
  ```
  It must bind `0.0.0.0` *inside* the container, otherwise the Docker proxy
  cannot reach it. Host exposure is restricted by publishing the port as
  `127.0.0.1:8088:8088` instead — ARI is not reachable from the LAN or from
  Windows.
- **`ari.conf` is not FreePBX-managed.** No module owns it, so the `[phonesys]`
  user is appended by hand and persists via the `etc_data` volume. Stock file
  kept as `ari.conf.orig` inside the container. Because it is outside FreePBX's
  control, it will *not* be recreated if the volume is ever wiped.

Verify both, not just one:

```bash
docker compose exec -T freepbx asterisk -rx "http show status"   # Server Enabled and Bound
docker compose exec -T freepbx asterisk -rx "ari show users"     # phonesys
```

The event WebSocket is `ws://127.0.0.1:8088/ari/events?app=<name>&subscribeAll=true`
with the same basic-auth credentials.

## One-way audio: `media_address` is mandatory here

**Symptom:** extension registers cleanly, you hear Asterisk's prompts, Asterisk
never hears you, and the call dies at *exactly* 30 seconds with:

```
res_pjsip_sdp_rtp.c: Disconnecting channel 'PJSIP/1001-...'
  for lack of audio RTP activity in 30 seconds
```

That 30s is `rtptimeout` firing because zero inbound RTP arrived.

**Cause:** Asterisk advertised its container address in the SDP —
`c=IN IP4 10.77.0.20` — which nothing outside the Docker VM can route to. The
softphone dutifully sent audio into a black hole. The reverse direction worked,
because the phone advertised a *reachable* address. That asymmetry is the
signature of this bug.

**Under Docker Desktop, Asterisk's automatic NAT rewrite cannot solve this.**
Every peer appears to come from `10.77.0.1`, and the only address Asterisk can
see on itself is `10.77.0.20`, so auto-detection has no correct answer available.
`external_media_address` and `local_net` are effectively dead controls — three
separate correctly-applied changes to them (verified live via
`pjsip show transport`, including a full restart to defeat `allow_reload=no`)
produced a byte-identical SDP.

**Fix:** bypass auto-detection with an explicit per-extension `media_address`.
It is an unconditional override applied while building the SDP — no NAT hook, no
transport lookup, no `local_net` test to pass:

```bash
docker compose exec -T db mysql -ufreepbxuser -p"$(cat secrets/freepbxuser_password.txt)" asterisk \
  -e "UPDATE sip SET data='100.75.207.86' WHERE keyword='media_address' AND id LIKE '10%';"
docker compose exec -T freepbx fwconsole reload
```

In the GUI this is **Extension → Advanced → Media Address**. It must be set on
**every new extension** — FreePBX leaves it blank by default.

The value is whatever address clients actually reach the PBX on (currently the
Tailscale IP `100.75.207.86`). If that changes, every extension needs updating —
which is the main argument for eventually doing the migration below.

Verify the SDP rather than trusting the setting:

```bash
docker compose exec -T freepbx asterisk -rx "pjsip set logger on"
# place a call to *43, then:
docker compose exec -T freepbx grep "c=IN IP4" /var/log/asterisk/full | tail
```

A good call shows `c=IN IP4 100.75.207.86` and lasts longer than 30 seconds.

Note the Docker UDP path itself is *fine* — a probe to a published RTP port
arrives in the container correctly. Only the advertised address was ever wrong.

## phonesys voice bridge mounts

Two bind mounts serve the `phonesys` voice-agent bridge (`~/work/phonesys`):

| Host | Container | Purpose |
|---|---|---|
| `~/work/phonesys/media/sounds` | `/var/lib/asterisk/sounds/en/phonesys` | TTS audio the bridge writes for playback |
| `~/work/phonesys/media/rec` | `/var/spool/asterisk/recording` | caller recordings Asterisk writes |

Both are needed because ARI's `play` takes a `sound:` URI resolved against
Asterisk's *own* filesystem — it accepts neither a host path nor a URL. The
bridge runs on the WSL host, so the two processes must share a directory.

Both paths nest inside the `var_data` named volume; the bind overlays the
volume at that path. Verify after any container recreate:

```bash
echo probe > ~/work/phonesys/media/sounds/probe.txt
docker compose exec -T freepbx cat /var/lib/asterisk/sounds/en/phonesys/probe.txt
docker compose exec -T freepbx sh -c 'echo back > /var/spool/asterisk/recording/probe.txt'
cat ~/work/phonesys/media/rec/probe.txt
```

Permissions work out without intervention: host files land as gid 1000, and
Asterisk runs as uid 999 / **gid 1000**, so group read is sufficient (`0644`).

## Known limitation

Docker Desktop masks source IPs: Asterisk sees traffic as coming from
`10.77.0.1` rather than real peers. NAT auto-detection misjudges as a result,
and **one-way audio is the expected failure mode on real trunk calls.**

The fix is to move off Docker Desktop:

1. Install `docker-ce` natively in this Ubuntu WSL distro (coexists with Docker
   Desktop; switch via `docker context use`).
2. Enable WSL mirrored networking in `%USERPROFILE%\.wslconfig`:
   ```ini
   [wsl2]
   networkingMode=mirrored
   ```
   then `wsl --shutdown`. Windows 11 25H2 / build 26200 supports this.
3. Switch the `freepbx` service to `network_mode: host` and drop the `ports:`
   block entirely.

That removes all Docker NAT and puts Asterisk directly on the LAN, leaving only
the home router — the normal case every FreePBX guide assumes. Mirrored mode is
effectively mandatory for this, because `netsh interface portproxy` is TCP-only
and cannot forward SIP/RTP UDP into a NAT'd WSL2 distro.

The Compose file and all FreePBX config carry over unchanged.

## Notes

- Secrets live in `secrets/` (gitignored), generated at setup.
- Postfix SASL credentials are a dummy placeholder; email notifications are not
  configured.
- State persists in the `mysql_data`, `var_data`, `etc_data` volumes.
