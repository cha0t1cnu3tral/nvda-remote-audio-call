# Remote Audio and Call 0.1.1 — call fixes preview

Install **remoteAudioCall-0.1.1.nvda-addon** on both computers and restart
NVDA. Connect through **Tools → Remote Access** with exactly one controller
and one controlled computer, then use **Tools → Remote Audio and Call**.

- Answering near the 30-second ringing timeout now gives the caller time to
  initialize audio devices, instead of ending the call during startup.
- Disconnect cleanup runs on NVDA's UI thread, so closing an incoming-call
  dialog and updating menus do not run on the Remote network thread. A delayed
  disconnect callback cannot cancel a newly attached connection.
- Report status explicitly explains when extra connected computers block calls.
- Choose microphone and speakers directly from the Tools submenu or incoming
  call window. The device controls have explicit accessible names.
- Playback now fills the initial WASAPI buffer before starting the device.
- Report status distinguishes microphone capture, missing remote audio,
  playback startup and a silent selected microphone using native health data.

Validation completed for this release:

- 38 automated tests, including near-timeout answers, both Remote roles,
  disconnect thread dispatch and stale disconnect callbacks.
- Calls initiated from each side, capability negotiation, duplex synthetic
  packets and hang-up through TLS to nvdaremote.com in a random private room.
  No microphone audio was transmitted during this relay test.
- The complete native duplex pipeline also passed through the public TLS
  relay: real microphone capture and Opus encoding on both ends, relay delivery,
  Opus decoding, and consumption by each WASAPI playback buffer. Microphones
  were muted before capture started and playback volume was zero during this
  check; it does not establish audible spoken-call quality.
- Real wxPython from NVDA 2026.2: menu creation, incoming-call dialog, Answer
  transition, microphone/speaker selection and teardown, with NVDA services
  stubbed and windows hidden.
- Local microphone/playback startup, mono Opus capture and shutdown under a
  non-administrator account. Microphone packets were discarded in memory.
- Helper codec self-test, device enumeration, process-loopback startup/shutdown,
  executable manifest/dependency inspection and Python compilation.

This remains a **preview**: interactive spoken calls between two NVDA
computers are not yet verified. Install the same version on both ends. Calls
require Windows x64 and NVDA 2025.1 or newer; computer audio excluding NVDA
requires Windows build 20348 or newer. Use headphones; echo cancellation is
not included. Everyone sharing the Remote key and the relay operator must be
trusted. Some relays may filter custom messages.

The packaged add-on and `SHA256SUMS.txt` are attached.
