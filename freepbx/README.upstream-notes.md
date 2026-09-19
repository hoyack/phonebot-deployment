# Historical upstream reference

The image originates from [escomputers/freepbx-docker](https://github.com/escomputers/freepbx-docker).
Use [the canonical Windows/PBX installation guide](../docs/WINDOWS-PBX.md), not historical local lab instructions.

This deployment uses Docker Desktop published UDP ports, not upstream host-iptables `run.sh`, and the `17-nofail2ban` image. Source NAT can obscure peer addresses; enforce access at the Windows/network boundary. Native Linux host networking is a different deployment, not a drop-in unchanged migration.

Old notes described one operator's preconfigured NAT, IP addresses, credentials, Ncat chain and media permissions. Those are not portable defaults. The supplied native PowerShell relay is single-call; the RTP port span is not a claim of streaming-call capacity. A registration-based trunk does not guarantee inbound SIP/RTP through every NAT or eliminate provider-specific firewall rules.
