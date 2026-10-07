# Remote Audio and Call 0.1.2: reconnect and simultaneous audio fixes

Install **remoteAudioCall-0.1.2.nvda-addon** on both computers and restart NVDA.

- Voice calls and shared computer audio now run together. Starting or answering
  a call preserves computer audio; starting audio during a call preserves the
  call. Microphone mute affects only voice. Stop audio or hang up stops both.
- A disconnect immediately closes both audio helpers and resets their sessions.
  After Remote reconnects, capability negotiation restores available controls.
  Start a new call or audio share after reconnecting.
- Fresh relay membership replaces stale participant IDs, fixing controls that
  could remain unavailable after reconnecting. Delayed disconnect events cannot
  reset a newer connection, including a reconnect using the same transport.
- A voice or computer-audio helper failure stops only the affected stream.

Validation: 50 automated tests; real NVDA 2026.2 wxPython menu/dialog checks
with NVDA services stubbed; simultaneous synthetic voice/computer-audio frames
through a public TLS relay; native mono/stereo codec self-test, device enumeration
and executable inspection. Native duplex calls also passed through the relay
in both directions, with microphones muted before capture and playback volume
zero. The native helper is unchanged from 0.1.1.

This remains a preview. Audible simultaneous music and conversation between two
physical NVDA computers have not been verified. Calls require Windows x64 and
NVDA 2025.1 or newer; computer audio excluding NVDA requires Windows build 20348
or newer. Install the same version on both ends.

The packaged add-on, SHA256SUMS.txt and Opus license are attached.
