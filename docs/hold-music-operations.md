# Hold music: installation and media-path evidence

## Runtime asset (not stored in Git)

- Class: `phonesys_hold`; alphabetical file order.
- Track: user-supplied “Tranquil Pads” by metadata artist asemili.
- Container file: `/var/lib/asterisk/moh/phonesys_hold/001-tranquil-pads.wav`.
- Host original and converted track: `~/work/phonesys/media/moh/phonesys_hold/`.
- Converted format: mono, 8000 Hz, signed 16-bit PCM WAV; 208.3335 seconds.
- WAV SHA-256: `30caee8c09b527ecd7412b9c0a2683c4d10228c849799f2416be2b977d747b8c`.
- Original MP3 is retained host-side only, not as a duplicate playlist entry.
- A moderate loudness conversion targets -20 LUFS; no subjective listening claim.

Merge the stanza from `freepbx/phonesys_hold.conf.example` into the container's
`/etc/asterisk/musiconhold_custom.conf`. Preserve other stanzas. That file is
included by `musiconhold.conf` and is outside FreePBX-generated configuration.
The `/etc` and `/var` paths are in persistent Docker volumes. Copy the track into
the class directory; set asterisk ownership and readable permissions. Use
`asterisk -rx 'moh reload'`, then check `moh show classes` and `moh show files`.
Do not restart the entire PBX or replace its default class. For fresh deployments,
restore the user-provided audio separately; a Git clone alone contains no music.

## Real Asterisk isolation probe

An isolated local probe exercised two temporary AudioSocket channels in a mixing
bridge, using an independent Stasis app. Both peers supplied digital silence.
MOH was started on the simulated **caller channel only**, never bridge-wide.
No PSTN, SIP trunk, physical telephone, or external recipient was called.

Observed audio:
- Before MOH: caller and STT legs both RMS 0.
- During MOH: caller RMS 2141.069; STT leg RMS 0 with samples received.
- After DELETE channel MOH: caller and STT legs both RMS 0.
- Probe channels/bridge removed; no active calls remained; temporary peer port
  19011 was confirmed closed afterward.

This verifies Asterisk-side channel-specific music isolation and stop behavior.
It does NOT rule out acoustic echo from a physical caller's speakerphone, nor
prove speech recognition/control handling on a human call. That remains part of
end-to-end acceptance.

Voice-progress and result audio must stop MOH before speaking. Resume MOH only
if the caller still wants it and research remains active. On all exit paths,
stop music and release only the resources owned by that call.
