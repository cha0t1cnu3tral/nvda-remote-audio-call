# Remote Audio and Call 0.1.3: usable controls with extra Remote computers

Install **remoteAudioCall-0.1.3.nvda-addon** on the two computers exchanging
voice or shared audio, then restart NVDA. Extra computers sharing the Remote
channel do not need this add-on.

- Extra controllers/controlled computers no longer disable the selected pair,
  including computers without the add-on. Ordinary Remote features continue.
- The sole compatible opposite-role computer is selected after a short discovery
  period. With multiple compatible peers, use **Choose remote computer...**.
  The accessible chooser lists role and Remote client ID; switching stops audio.
- Calls and audio packets are addressed to the intended recipient. Other add-on
  computers do not ring or play them. Audio still travels through the existing
  Remote relay; broadcast relays and everyone sharing the key remain trusted.
- Start call stays usable without a connection and explains what is missing.
  Settings, status, device selection and computer selection remain accessible.
  Report status identifies the selected computer or the availability reason.
- Selected-peer disconnects reset both streams; reconnects rediscover the same
  running add-on even with a new Remote ID. Stale connection, membership and
  invitation callbacks cannot reset or modify the new connection. Start audio
  again after reconnecting; microphones do not reopen automatically.
- Simultaneous voice and computer audio, independent failure cleanup and
  microphone mute are preserved.

This release uses Remote extension protocol v2. Both audio endpoints need
0.1.3 or newer; versions 0.1.0 through 0.1.2 cannot exchange audio with it.
Extra computers without a compatible add-on are excluded from audio discovery.

Validation: 68 automated tests; real NVDA 2026.2 wxPython menu, chooser and
reconnect checks with NVDA services stubbed; three-participant public TLS
relay checks with both plain and add-on observers, simultaneous synthetic
streams, busy responses and selected-peer reconnect; native duplex relay
calls muted from startup with playback volume zero; native codec, device and
executable checks. The native helper is unchanged from 0.1.2.

This remains a preview: audible music and conversation between two physical
NVDA computers have not been verified. Windows x64 and NVDA 2025.1 or newer
are required; computer audio excluding NVDA requires Windows build 20348 or
newer. The add-on package, SHA256SUMS.txt and Opus license are attached.
