# Clean installation: FreePBX + Windows AudioSocket + WSL

Canonical guide for this repository's Docker Desktop/WSL topology. These commands are for a **new installation**, not an unattended updater. Do not run them against an existing PBX without a private backup and an approved maintenance window. This publication was prepared with read-only deployment inspection; no services, firewalls, relay or calls were changed.

## 0. Start with a new Windows host

Use a supported Windows release with hardware virtualization enabled. Follow the
[Microsoft WSL prerequisites](https://learn.microsoft.com/windows/wsl/install)
and [Docker Desktop Windows requirements](https://docs.docker.com/desktop/setup/install/windows-install/),
including organizational licensing requirements. Do not change a working host's
WSL networking mode as part of this procedure.

In **Administrator PowerShell on the new host**:

```powershell
wsl.exe --install -d Ubuntu
# Reboot Windows if requested; then launch Ubuntu and create its Unix user/password.
wsl.exe --update
wsl.exe --list --verbose
# Only if this new distro is listed as version 1:
wsl.exe --set-version Ubuntu 2
```

Use the exact installed distro name if it is not `Ubuntu`. In that distro,
check `ps -p 1 -o comm=`. If PID 1 is not systemd, use `sudoedit /etc/wsl.conf`
and merge the following into its existing configuration (do not replace
unrelated sections):

```ini
[boot]
systemd=true
```

Close the new distro's work and run `wsl.exe --shutdown` in Windows PowerShell,
then reopen it. **Shutdown stops all WSL distros**, so do this only during the
new-host setup or an approved maintenance window. Verify in Ubuntu:

```bash
ps -p 1 -o comm=
systemctl --user is-system-running
```

PID 1 must be `systemd`; investigate failed user units if the second command
reports `degraded`, rather than disabling systemd. Install Docker Desktop using
the official Windows installer, choose Linux containers and the WSL2 engine,
then enable **Settings → Resources → WSL Integration → the chosen distro**.
Apply/restart Docker Desktop and verify from that distro:

```bash
docker version
docker compose version
```

Both client and server must answer. Do not install a second Docker daemon inside
WSL for this topology. Select Docker Desktop start-at-sign-in if desired; this
is still a user-session deployment, not pre-login server availability.

Continue with [SETUP.md sections 1–3](SETUP.md) for the private checkout,
Hermes and bridge dependencies, then return here for PBX provisioning. Use your
own GitHub SSH/credential-manager login with access to
`https://github.com/hoyack/phonebot-deployment`; never embed a token in a clone URL.

## 1. Scope, prerequisites and topology

Install a supported Windows/WSL2 distribution with systemd, Docker Desktop Linux containers, Docker Compose v2 and Docker Desktop integration enabled for the **same WSL distribution and Windows user** that own the bridge. Install Python/bridge/Hermes prerequisites using [SETUP.md](SETUP.md) and the repository installer (`scripts/install.sh --help`). Keep the checkout on the WSL Linux filesystem, not a Windows-mounted directory: Unix permissions matter. Replace all `<...>` placeholders; never paste a placeholder unchanged.

```text
trusted SIP phone → Windows UDP 5060 + RTP 10000–10099 → Docker FreePBX
WSL phonesys → host loopback TCP 8088 → Asterisk ARI (app phonesys)
Asterisk → host.docker.internal:19010 → native Windows PowerShell relay
         → dynamically discovered WSL IPv4:9010 → phonesys
phonesys → loopback Hermes gateways 8642 / 8643 / 8644
```

The shipped relay supports **one active streaming call across 7001–7003**, including transfers within that call. Additional connections may queue and time out; it is not a busy-response queue or a multi-call service. Limit incoming calls at the PBX/operational level. The RTP port count does not establish agent capacity. AudioSocket is raw, unauthenticated TCP without TLS; never expose it to untrusted networks.

| Extension | Profile / gateway | Role |
|---|---|---|
| 7000 | phonebot / 8642 | Turn-based fallback, shared media files |
| 7001 | phonebot / 8642 | Mary Yoder streaming receptionist |
| 7002 | jyoder / 8643 | John Yoder, `JOHN_YODER_PIN` |
| 7003 | atlas / 8644 | Private Atlas, `ATLAS_PIN`, optional consent-based hold |

PIN checks are in the bridge, not these dialplan stanzas. Use independent profile/API credentials and nonempty PINs; direct dialing and receptionist transfers both need authorized negative/positive acceptance tests. Directory visibility does not grant access. Never copy existing profile state, memories, tokens or recordings into a fresh install. No carrier, outbound route or PSTN call is required.

## 2. Discover and reserve networking first

In Windows PowerShell:

```powershell
wsl.exe --list --verbose
wsl.exe --list --quiet
wsl.exe -d '<EXACT-DISTRO-NAME>' -- hostname -I
Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 8080,8443,8088,19010
Get-NetUDPEndpoint | Where-Object { $_.LocalPort -eq 5060 -or ($_.LocalPort -ge 10000 -and $_.LocalPort -le 10099) }
Get-NetIPConfiguration
Get-NetConnectionProfile
```

In WSL:

```bash
export REPO='/absolute/path/to/checkout'
cd "$REPO/freepbx"
docker context show
docker compose version
docker ps --format 'table {{.Names}}\t{{.Ports}}'
ip -4 route
docker network ls
# Inspect selected networks, not container environment (which can contain secrets).
docker network inspect <NETWORK-NAME> --format '{{json .IPAM.Config}}'
```

The Compose defaults use 10.77.0.0/24 (.1 gateway, .10 database, .20 PBX). If this overlaps LAN, VPN, WSL or another Docker network, set **all four** `PBX_SUBNET`, `PBX_GATEWAY`, `PBX_DB_IP`, `PBX_IP` consistently before creation. Record chosen values in a private local `freepbx/.env`, together with `PHONESYS_DIR` if the bridge is not `../phonesys`. Relative bind paths resolve from `freepbx/`, not your shell's original directory. An absolute `PHONESYS_DIR` supports arbitrary checkout locations. Do not change a running stack's subnet casually.

GUI 8080/8443 and ARI 8088 default to host loopback. Local Windows processes may reach Docker Desktop loopback publications; “loopback-only” does **not** exclude Windows. Keep Hermes gateway ports loopback-only in WSL. SIP/RTP are published on all host interfaces: restrict them **before** bringing up the PBX. Never open database 3306, ARI or gateway ports to LAN/WAN. Use a VPN for remote phones/admin; do not expose the GUI directly to the Internet.

## 3. Secrets, database initialization and GUI

The Compose images are `mariadb:10.11` and `escomputers/freepbx:17-nofail2ban`. Tags can move; pin the `image:` entries to reviewed digests for reproducible deployments. Read-only inspection of the existing installation identified:

* `escomputers/freepbx@sha256:34e65b7a0a02ad9da67315c12bcfd0368a89c3f6d185abf498ae8a48687f0daa`
* `mariadb@sha256:be981e4113326ada8d6004174dd09eeaefc03094037f811182a52d4f2e737350`

These identify observed running images, **not a freshly rebuilt host or newly executed DB initialization**. Review architecture/security updates before adopting them. This third-party PBX image is not an official Sangoma appliance. The nofail2ban variant requires external access controls; Docker NAT may collapse source IPs and make PBX-side bans unsafe.

Prepare only new local secrets/media (the helper defaults to dry-run, refuses existing secrets/media directories and symlink paths, never invokes Docker):

```bash
cd "$REPO/freepbx"
umask 077
chmod 700 secrets
python3 prepare.py --phonesys-dir "$REPO/phonesys"
python3 prepare.py --phonesys-dir "$REPO/phonesys" --apply
# If PHONESYS_DIR is overridden, supply that exact absolute path instead.
# Already-created media dirs: inspect/reuse manually, never delete them to pass a guard.
# No secret contents are printed; SMTP secret is empty until mail is provisioned.
docker compose config --quiet
docker compose pull
docker compose up -d db
docker compose exec -T db healthcheck.sh --connect --innodb_initialized
docker compose up -d freepbx
docker compose ps
```

Wait for database health before proceeding. `init.sql` creates `asterisk` and `asteriskcdrdb` with utf8mb4 and grants `freepbxuser` access on **first empty-volume initialization only**. MariaDB's entrypoint creates the password-bearing `freepbxuser`@`%` account before this SQL runs; no password is embedded in SQL, and no global privileges are granted. The database server address is `db`, but the grant host is the connecting PBX, **not** `db` or `localhost`. `%` is confined by an unpublished database port and the dedicated Compose network; do not attach untrusted containers. It does not migrate existing volumes or rotate passwords. Never use `down -v` to fix a failed install on data you need. Compose refuses to auto-create missing media bind sources, so path typos fail instead of creating root-owned directories.

Run the guarded image installer once on a fresh PBX. It defaults to dry-run, requires a generator-format private DB secret, checks DB health and refuses an existing or partial `/etc/freepbx.conf` or `/etc/amportal.conf`. It streams the DB password on stdin, not through your host command line or shell history. The image installer ultimately receives a password argument inside the container; trusted container administrators can inspect processes. Do not enable shell tracing or session recording; never paste installer diagnostics publicly without redaction.

```bash
bash install-fresh.sh          # no Docker commands or changes
# Do not pass --apply until the two-schema connection check below passes.
```

The explicit installer arguments are `--dbhost=db --dbport=3306 --dbname=asterisk --cdrdbname=asteriskcdrdb --dbuser=freepbxuser`. Root is for MariaDB bootstrap/maintenance only (`MARIADB_ROOT_HOST=localhost`); never pass it to FreePBX or use the installer’s `--rootdb` shortcut, which can create grants against the wrong host in a split-container topology.

Before installing, check the two schemas **from the PBX container**, not only from MariaDB localhost (read-only connections, no tables or calls):

```bash
docker compose exec -T freepbx php -r '
$password = rtrim(stream_get_contents(STDIN), "\r\n");
foreach (["asterisk", "asteriskcdrdb"] as $name) {
    try {
        $db = new PDO("mysql:host=db;port=3306;dbname=".$name, "freepbxuser", $password);
        $db->query("SELECT 1");
        echo $name." connection OK\n";
    } catch (Throwable $e) {
        fwrite(STDERR, "Database connection failed; check private DB health/grants/secret.\n");
        exit(1);
    }
}' < secrets/freepbxuser_password.txt
```

Do not print `SHOW GRANTS` unredacted: it can include password hashes. The inspected MariaDB entrypoint's `docker_setup_db` creates the configured user with its secret and host **before** `docker_process_init_files`; it creates that user even without `MYSQL_DATABASE`. Our SQL then creates/grants both required schemas. Health alone does not prove those grants.

Some inspected image code returns nonzero even after its success message: **do not retry automatically**, and never use `--force`. Inspect the private log, configuration and GUI to establish the actual result. Failed/partial new installs require diagnosis or a deliberately discarded disposable stack, not blind reruns on persistent data.

After both schema checks pass, explicitly install:

```bash
bash install-fresh.sh --apply  # new PBX only; no --force / --rootdb
```

Open `http://localhost:8080` locally and create a **new strong GUI administrator**, set timezone and notification email only if actually provisioned. Prefer validated HTTPS before remote administration; port 8443 alone does not imply a trusted certificate. Complete first-run prompts and required module installation/updates. Apply Config and confirm Dashboard reports Asterisk/database connectivity. Keep a private inventory of module versions. Database connection settings persist in the PBX `/etc` volume; do not commit them. Do not rerun the installer blindly after GUI configuration.

## 4. SIP, RTP, NAT and a real internal phone extension

In **Settings → Asterisk SIP Settings**:

* Set RTP start/end to **10000 / 10099**, matching Compose exactly.
* Enable the intended PJSIP UDP transport on 5060. Disable anonymous/guest SIP and unneeded transports/services. Use G.711 ulaw/alaw for the initial phone test; DTMF RFC4733 (often labeled RFC2833).
* Set External Address to the reachable Windows host/VPN or public address required by your actual clients, not a copied historical address. Add only actual local networks: selected Docker subnet, WSL subnet and trusted LAN/VPN as appropriate. Inspect advertised SDP during an authorized internal call; auto-detection behind Docker Desktop may advertise an unreachable container address.
* In **Applications → Extensions**, add an ordinary PJSIP endpoint such as **1001**, unique strong SIP secret, `from-internal` context, no direct media, RTP symmetric / force rport / rewrite contact enabled where NAT requires them. Register a softphone to the Windows host's reachable LAN/VPN IP:5060. Do not create PJSIP endpoints 7000–7003: those are custom dialplan destinations.
* If SDP still advertises a Docker-only address, set the phone extension's **Advanced → Media Address** to the address that phone can reach. This is topology-specific, not a universal mandatory setting. Do not issue bulk SQL edits to FreePBX tables. LAN/VPN and carrier endpoints may need different media addresses.

Apply Config; transport changes may need an Asterisk restart in an approved idle window. Verify effective settings rather than assuming database edits took effect:

```bash
docker compose exec -T freepbx asterisk -rx 'rtp show settings'
docker compose exec -T freepbx asterisk -rx 'pjsip show transports'
docker compose exec -T freepbx asterisk -rx 'pjsip show endpoints'
docker compose exec -T freepbx asterisk -rx 'pjsip show contacts'
```

Firewall: permit UDP 5060 and 10000–10099 only from trusted phone/VPN networks (and provider-published ranges if a carrier is later authorized). Docker Desktop source translation can prevent distinguishing original peers inside Asterisk. Disable router SIP ALG if it interferes, but validate with your router/provider. TCP `netsh portproxy` cannot forward SIP/RTP UDP. Registration does not guarantee media reachability or eliminate NAT/firewall work. Do not switch Docker networking modes as an untested shortcut.

## 5. HTTP, ARI and four-extension dialplan

Use FreePBX settings for generated HTTP config, never edit `http_additional.conf`:

```bash
docker compose exec -T freepbx fwconsole setting HTTPENABLED 1
docker compose exec -T freepbx fwconsole setting HTTPBINDADDRESS 0.0.0.0
docker compose exec -T freepbx fwconsole setting HTTPBINDPORT 8088
```

The bind is inside the container; Compose restricts host exposure. Inspect `/etc/asterisk/ari.conf` **privately** first. Depending on installed modules, ARI may be GUI-managed with includes. Prefer **Settings → Asterisk REST Interface Users** if present: create user `phonesys`, read/write access, a unique password, and enable ARI. Otherwise merge `freepbx/ari.conf.example` into a private copy of the existing `ari.conf`, retaining includes and a single `[general]`; replace the placeholder, install with asterisk-readable restrictive permissions. Do not overwrite GUI-generated ARI user files. Set matching `ARI_USER`/`ARI_PASSWORD` in the bridge's protected environment per SETUP.md (the setup generator supplies a fresh `ARI_PASSWORD`; use that same value for this PBX user rather than generating a second mismatched secret). No wildcard browser CORS origin is needed.

Privately back up `/etc/asterisk/extensions_custom.conf`, then merge `freepbx/extensions_custom.conf.example`: put the include in the **existing** `[from-internal-custom]` section if one exists, and add one `[phonesys-custom]` section. Never replace unrelated routing or duplicate contexts. For a completely fresh empty custom file, the example is the full content. Transfer the reviewed file with `docker compose cp <PRIVATE-FILE> freepbx:/etc/asterisk/extensions_custom.conf` and retain asterisk readability. Do not blindly append on every run.

```bash
docker compose exec -T freepbx fwconsole reload
docker compose exec -T freepbx asterisk -rx 'http show status'
docker compose exec -T freepbx asterisk -rx 'ari show users'
docker compose exec -T freepbx asterisk -rx 'module show like audiosocket'
docker compose exec -T freepbx asterisk -rx 'dialplan show phonesys-custom'
# Interactive curl prompts for ARI password; do not put it in the command line.
curl --fail --user phonesys http://127.0.0.1:8088/ari/asterisk/info
```

Expect HTTP bound to container 0.0.0.0:8088, ARI user phonesys, AudioSocket modules loaded, and four extensions entering `Stasis(phonesys,7000)` through `Stasis(phonesys,7003)`. Confirm app registration after starting the bridge:

```bash
curl --fail --user phonesys http://127.0.0.1:8088/ari/applications
```

In **Admin → Custom Destinations**, create targets `phonesys-custom,7000,1`, `phonesys-custom,7001,1`, `phonesys-custom,7002,1`, `phonesys-custom,7003,1`, named distinctly. Disable Return unless you intentionally design a return route (these stanzas hang up). Internal phones use the `from-internal-custom` include. If an authorized carrier is added later, choose Mary’s custom destination in **Connectivity → Inbound Routes**, matching the exact provider DID/CID rather than a broad catch-all; test only with permission. Never route all unknown DIDs straight to a private assistant. Carrier setup is optional: use provider-specific PJSIP authentication/registration, allowed source ranges and codecs, unique credentials, and no outbound routes unless explicitly needed. No PSTN account or call was used to validate this guide.

## 6. Shared media permissions

Compose mounts bridge `media/sounds` at `/var/lib/asterisk/sounds/en/phonesys`, and `media/rec` at `/var/spool/asterisk/recording`. ARI sound paths are container paths, not WSL paths. Both overlay `/var` volume directories; backing up that volume alone does not capture these host binds.

```bash
docker compose exec -T freepbx id asterisk
id
```

Discover numeric IDs; do not assume them (the inspected image used UID 999/GID 1000). For new dedicated directories, make the bridge user owner and the discovered Asterisk GID the group, mode 2770, with group-writable files or a default ACL. Example after substituting the **verified** numeric group:

```bash
sudo chown "$(id -u):<ASTERISK-GID>" ../phonesys/media/sounds ../phonesys/media/rec
sudo chmod 2770 ../phonesys/media/sounds ../phonesys/media/rec
# Optional default ACLs, if setfacl is installed:
sudo setfacl -m d:u::rwx,d:g::rwx,d:o::--- ../phonesys/media/sounds ../phonesys/media/rec
```

If the GID differs from the bridge's groups, grant the bridge user an ACL or membership in a deliberately created matching group and restart its session/service. Do not recursively chown whole PBX volumes or use 777. Test **as Asterisk**, not root, on new non-sensitive probes; remove them afterward:

```bash
printf 'media-probe\n' > ../phonesys/media/sounds/.permissions-probe
chmod 640 ../phonesys/media/sounds/.permissions-probe
docker compose exec -T --user asterisk freepbx sh -c 'umask 007; test -r /var/lib/asterisk/sounds/en/phonesys/.permissions-probe && touch /var/spool/asterisk/recording/.permissions-probe'
test -w ../phonesys/media/rec/.permissions-probe
rm ../phonesys/media/sounds/.permissions-probe ../phonesys/media/rec/.permissions-probe
```

Adapt paths to PHONESYS_DIR when overridden. Treat recordings, transcripts, synthesized speech, logs and backups as private data; define consent, retention and deletion policies before calls.

## 7. Windows relay and logon startup

The canonical helper is [start-audiosocket-proxy.ps1](../windows/start-audiosocket-proxy.ps1), not an Ncat/cmd pipe or a static WSL-IP portproxy. It sets TCP NoDelay on both legs, copies each direction independently and closes both sockets when either side finishes. It discovers WSL IPv4 per connection, bounds discovery/connect waits, refuses ambiguous multiple addresses and prevents duplicate listeners. NAT-mode WSL with one non-loopback IPv4 is the supported baseline; mirrored/multi-interface networking needs deliberate adaptation, not the first arbitrary IP.

Copy only `windows/*.ps1` to a stable **Windows** directory, e.g. `$env:LOCALAPPDATA\PhoneSys` (not a UNC working directory). Run under the Windows user that owns the distro. Do not run as SYSTEM: that user generally has a different/no WSL registration. Review execution policy (`Get-ExecutionPolicy -List`); use an organization-approved signing/policy workflow, not blanket policy bypass.

```powershell
$RelayDir = Join-Path $env:LOCALAPPDATA 'PhoneSys'
$Distro = '<EXACT-DISTRO-NAME>'
# After copying the reviewed files into $RelayDir:
& "$RelayDir\start-audiosocket-proxy.ps1" -Distro $Distro -ListenAddress '0.0.0.0' -ListenPort 19010 -TargetPort 9010
# Ctrl+C stops this foreground instance. Do not leave two instances running.
```

Bind to a specific Docker-reachable Windows address if stable; `127.0.0.1` may not be reachable via `host.docker.internal` from the PBX. With `0.0.0.0`, a narrowly scoped firewall rule is mandatory. Discover the Docker-to-Windows source/interface rather than assuming the WSL subnet or `LocalSubnet` is sufficient. From an **elevated** PowerShell only after replacing the reviewed source and profile:

```powershell
New-NetFirewallRule -DisplayName 'PhoneSys AudioSocket Docker only' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 19010 -RemoteAddress '<DOCKER-SOURCE-IP-OR-CIDR>' -Profile '<Domain-or-Private-or-Public-as-actually-observed>' -Program "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
Get-NetFirewallRule -DisplayName 'PhoneSys AudioSocket Docker only' | Get-NetFirewallAddressFilter
```

Do not allow Any remote address, turn off Windows Firewall or accept a broad automatic PowerShell allow rule. Audit existing broad rules, default inbound policy, Windows Hyper-V/WSL firewall and VPN ACLs too. Restrict WSL TCP 9010 to the actual relay path if enforcing a WSL firewall. Rules vary across Docker Desktop/Windows versions and must be verified on the new host.

Register the current-user **logon** task (registration does not start it):

```powershell
& "$RelayDir\register-audiosocket-task.ps1" -Distro $Distro -WhatIf
& "$RelayDir\register-audiosocket-task.ps1" -Distro $Distro
Get-ScheduledTask -TaskName 'PhoneSys-AudioSocket' | Select-Object TaskName,State,Actions,Principal
Start-ScheduledTask -TaskName 'PhoneSys-AudioSocket'
Get-ScheduledTaskInfo -TaskName 'PhoneSys-AudioSocket'
Get-NetTCPConnection -LocalPort 19010 -State Listen
```

The task is limited-user, interactive-logon, no execution time limit, IgnoreNew, with failure restart. It refuses to overwrite an existing task. Use Task Scheduler Operational logs for startup failures; inspect the exact task action and script ACLs. No passwords are stored in the task. **This is not unattended pre-login boot service**: Docker Desktop and user WSL/gateways must be available under that user. Configure Docker Desktop start-at-sign-in, enable WSL systemd and user-service lingering per SETUP.md. Sign-in is part of reboot acceptance. Do not enable automatic Windows login merely to make a server unattended; use a separately designed service/Linux-host topology if pre-login availability is required.

## 8. Dedicated consent-based hold music

Atlas can use `hold_enabled`, `hold_offer_seconds`, `hold_feedback_seconds` and `hold_moh_class: phonesys_hold` in bridge YAML. Preserve consent behavior and other extensions' defaults; provisioning music is not permission to force callers onto hold. Hold controls/limitations are documented with bridge setup; do not claim unimplemented DTMF callbacks. The example adds **only** `phonesys_hold`, leaving PBX default music untouched.

Use cleared/licensed local audio; do not download proprietary playlists or ship production tracks. On a new install with ffmpeg available:

```bash
mkdir -p ../phonesys/media/moh/phonesys_hold
ffmpeg -i '<LICENSED-LOCAL-AUDIO>' -map 0:a:0 -vn -af loudnorm=I=-20:TP=-3:LRA=7 -ac 1 -ar 8000 -c:a pcm_s16le ../phonesys/media/moh/phonesys_hold/001-hold.wav
ffprobe -v error -show_entries stream=codec_name,sample_rate,channels -of default=noprint_wrappers=1 ../phonesys/media/moh/phonesys_hold/001-hold.wav
docker compose exec -T freepbx install -d -o asterisk -g asterisk -m 750 /var/lib/asterisk/moh/phonesys_hold
docker compose cp ../phonesys/media/moh/phonesys_hold/001-hold.wav freepbx:/var/lib/asterisk/moh/phonesys_hold/001-hold.wav
docker compose exec -T freepbx chown asterisk:asterisk /var/lib/asterisk/moh/phonesys_hold/001-hold.wav
docker compose exec -T freepbx chmod 640 /var/lib/asterisk/moh/phonesys_hold/001-hold.wav
```

Back up and merge `musiconhold_custom.conf.example` into `/etc/asterisk/musiconhold_custom.conf`; ensure the main `musiconhold.conf` includes that custom file. Do not edit generated additional files or install both MP3/WAV copies as separate playlist entries. Numeric prefixes plus `sort=alpha` establish order.

```bash
docker compose exec -T freepbx asterisk -rx 'moh reload'
docker compose exec -T freepbx asterisk -rx 'moh show classes'
docker compose exec -T freepbx asterisk -rx 'moh show files'
docker compose exec -T --user asterisk freepbx test -r /var/lib/asterisk/moh/phonesys_hold/001-hold.wav
```

Retain originals privately; hashes/format/readability do not prove listening quality, consent flow or STT isolation. Authorize an internal human call to verify hold offers, explicit acceptance, music stop before speech/status, interruption and hangup cleanup. No live MOH asset or setting was changed while preparing this guide.

## 9. Backup, restore and reboot acceptance

Back up before module/image upgrades. Keep encrypted, access-controlled backups **outside Git**, including:

* A consistent DB dump or cold `mysql_data` volume; never tar a live MariaDB datadir and call it consistent.
* `etc_data` and `var_data`, which include ARI/SIP secrets, custom dialplan, FreePBX settings, certificates and dedicated MOH assets.
* Both host media bind directories separately, with an explicit recording-retention policy.
* Private local secret files, chosen Compose environment, image digests, module versions and Windows scripts/task export. Back up profile state separately under its owner's policy; never publish it.

Use FreePBX **Admin → Backup & Restore** with its database/custom-file coverage reviewed, or the repository runtime backup helper after reading its scope. A database-only dump or a FreePBX GUI backup alone may omit Docker-mounted data. For a manual **cold** snapshot: stop ingress, wait for `core show channels count` to report zero, stop bridge and PBX before DB, then export the exact discovered volumes with numeric ownership preserved. These are maintenance actions, not part of this read-only publication validation. Inventory exact mounts first:

```bash
docker inspect "$(docker compose ps -q freepbx)" --format '{{json .Mounts}}'
docker inspect "$(docker compose ps -q db)" --format '{{json .Mounts}}'
```

Restore rehearsal must target a separate isolated host/project, no carrier registration, no production ports and no production gateway credentials. Match image/module versions first; restore named volumes with original numeric ownership, secrets and bind directories to the paths expected by that project's Compose. Start restored DB and check health, then PBX. Do **not** run init.sql or the fresh installer against restored databases. Recheck paths, NAT, media permissions, ARI credentials, custom routing and music; rotate secrets if a backup was exposed. Do not overwrite the only known-good backup until rehearsal passes. Export the Windows task privately with `Export-ScheduledTask -TaskName 'PhoneSys-AudioSocket'`; a fresh host should register its own user/distro task rather than importing old account identities blindly.

After an approved Windows reboot and sign-in, without placing any call:

```powershell
wsl.exe --list --verbose
Get-ScheduledTask -TaskName 'PhoneSys-AudioSocket'
Get-ScheduledTaskInfo -TaskName 'PhoneSys-AudioSocket'
Get-NetTCPConnection -LocalPort 19010 -State Listen
```

```bash
cd "$REPO/freepbx"
docker compose ps
docker compose exec -T db healthcheck.sh --connect --innodb_initialized
docker compose exec -T freepbx asterisk -rx 'core show channels count'
docker compose exec -T freepbx asterisk -rx 'dialplan show phonesys-custom'
docker compose exec -T freepbx asterisk -rx 'rtp show settings'
docker compose exec -T freepbx asterisk -rx 'moh show classes'
systemctl --user is-active phonesys-bridge hermes-phonebot hermes-jyoder hermes-atlas
ss -ltn '( sport = :9010 )'
curl --fail --user phonesys http://127.0.0.1:8088/ari/applications
```

See SETUP.md for gateway health endpoints and installer validation. Check Docker resolution of `host.docker.internal` with `docker compose exec -T freepbx getent ahostsv4 host.docker.internal`. A listener/health response proves readiness only, not bidirectional media. Even a TCP port probe briefly consumes this single-call relay; do it only idle. With explicit authorization, use the internal SIP extension (not PSTN) to test 7000–7003, wrong/missing/correct PINs, Mary transfers, sustained duplex audio/barge-in, Atlas consent hold and hangup cleanup. Capture no private speech unnecessarily. Record configured routing, control-plane health, internal media acceptance and reboot recovery as **separate** results.

### Manual cold volume snapshot/restore commands

These are optional maintenance commands, **not** publication validation commands. Use them only on an authorized idle deployment, with a trusted locally available `alpine:3.20` helper image (record/pin its digest). All backup paths must be private absolute Linux paths outside the checkout. Stop the bridge first; coordinate any other writers. The following records the exact volumes from this Compose project instead of guessing project-prefixed names:

```bash
cd "$REPO/freepbx"
export BACKUP='/private/absolute/backup-directory'
mkdir -m 700 "$BACKUP"
export PBX_CID="$(docker compose ps -q freepbx)"
export DB_CID="$(docker compose ps -q db)"
export ETC_VOL="$(docker inspect "$PBX_CID" --format '{{range .Mounts}}{{if eq .Destination "/etc"}}{{.Name}}{{end}}{{end}}')"
export VAR_VOL="$(docker inspect "$PBX_CID" --format '{{range .Mounts}}{{if eq .Destination "/var"}}{{.Name}}{{end}}{{end}}')"
export DB_VOL="$(docker inspect "$DB_CID" --format '{{range .Mounts}}{{if eq .Destination "/var/lib/mysql"}}{{.Name}}{{end}}{{end}}')"
test -n "$ETC_VOL" && test -n "$VAR_VOL" && test -n "$DB_VOL"
systemctl --user stop phonesys-bridge
docker compose exec -T freepbx asterisk -rx 'core show channels count'
# Proceed ONLY after personally confirming zero channels and blocked ingress.
docker compose stop freepbx
docker compose stop db
for volume in "$ETC_VOL" "$VAR_VOL" "$DB_VOL"; do
  docker run --rm --network none -v "$volume:/source:ro" -v "$BACKUP:/backup" alpine:3.20 sh -c 'umask 077; tar -C /source -czf "/backup/$1.tgz" .' sh "$volume"
done
# Include host binds and local secrets separately; adjust overridden media path.
sudo tar -C ../phonesys -czpf "$BACKUP/host-media.tgz" media
sudo tar -C . -czpf "$BACKUP/local-secrets.tgz" secrets
sudo chmod 600 "$BACKUP/host-media.tgz" "$BACKUP/local-secrets.tgz"
# Copy private .env/Compose selections and record image digests separately.
# Encrypt with your approved backup system before off-host storage.
docker compose up -d db
docker compose exec -T db healthcheck.sh --connect --innodb_initialized
# Wait/retry health until successful before restarting the remaining services.
docker compose up -d freepbx
systemctl --user start phonesys-bridge
```

Require each export to exit successfully; verify archives with `tar -tzf` privately, checksums and restore rehearsal. A cold snapshot contains private data even if encrypted later. For restore, first create an **isolated** Compose project using the same image digests and configuration, with no production port collisions. Render `docker compose config --format json` privately to discover that isolated project's `volumes.*.name`, and create each named volume with `docker volume create <EXACT-ISOLATED-VOLUME-NAME>` **before creating any containers**. Map source etc/var/mysql archives explicitly to those names. Do not use `docker compose create` first: Docker may seed `/etc` and `/var` volumes from image contents, making them nonempty. Restore only to verified **empty** volumes (never into a running service):

```bash
# Repeat for each explicitly mapped etc / var / mysql target volume.
export TARGET_VOLUME='<ISOLATED-EMPTY-TARGET-VOLUME>'
export ARCHIVE='<MATCHING-SOURCE-VOLUME>.tgz'
docker run --rm --network none -v "$TARGET_VOLUME:/target" -v "$BACKUP:/backup:ro" alpine:3.20 sh -c 'test -z "$(ls -A /target)" || exit 1; tar -C /target -xzpf "/backup/$1"' sh "$ARCHIVE"
# In the isolated checkout, after confirming these directories are empty:
sudo tar -C ../phonesys -xzpf "$BACKUP/host-media.tgz"
sudo tar -C . -xzpf "$BACKUP/local-secrets.tgz"
```

Use a shell with fail-fast handling or check every status manually; do not proceed from a failed extraction. Restore private Compose variables separately; avoid carrying production gateways/carrier connections into the rehearsal. Start DB and verify health, then PBX, then connect only isolated test services. Apply the verification checklist above and validate restored custom dialplan, ARI users, MOH and media permissions. This procedure has not been executed against live volumes during publication.

## Verification provenance

The image installer’s supported DB flags and `HTTPBINDPORT` setting were inspected read-only. On the inspected image, `php install --help` printed its option list and then failed with a PHP return-type error in `FreePBXHelpCommand`; this is an upstream help-command defect, not evidence that a fresh installation succeeds or fails. No installer execution was attempted beyond help.

Offline follow-up audit: `python3 -m unittest discover -s freepbx -p 'test_*.py' -v` passes seven tests (dry-run no writes, private generated files, overwrite/symlink/media refusal, installer no-Docker dry-run, SQL/dialplan contract). `bash -n freepbx/install-fresh.sh` and the Windows PowerShell parser pass. Temporary Compose rendering covers the two explicit media binds, path-with-spaces override, three secrets, unpublished DB and loopback ARI. SQL statements were reviewed against the actual MariaDB entrypoint and FreePBX installer source, **not executed against a fresh database**. The inspected live configuration uses `AMPDBHOST=db`, `AMPDBNAME=asterisk`, `AMPDBUSER=freepbxuser`; only those non-secret fields were read out.

The publication author read the live Docker topology, selected non-secret bridge fields, loaded four-extension dialplan, HTTP status, AudioSocket module list, RTP range, MOH classes, Asterisk identity and native Windows relay source. PowerShell files were checked with the real Windows PowerShell parser; Compose was rendered in temporary directories using dummy secret files, never against live volumes. These checks do not constitute a fresh-host install, scheduled-task/firewall execution, restore rehearsal, reboot test or telephone acceptance. Commands that configure/install are documented operator steps, not claims that they were executed during publication.
