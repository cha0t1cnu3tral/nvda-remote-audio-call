# Remote Audio and Call 0.1.0 — preview

Download **remoteAudioCall-0.1.0.nvda-addon**, open it on each computer, and
restart NVDA. Controls appear in **NVDA → Tools → Remote Audio and Call**.

- Separate **Call** and **Audio** modes.
- Two-way microphone calls with ringing, Answer, Decline, mute and hang-up.
- The controlled person starts computer audio sharing; the controller hears
  application audio with NVDA's process and child speech processes excluded.
- No administrator access, service, driver, extra server or separate runtime.
- Device/volume settings and automatic stop on disconnect, lock or failure.
- Source repository is public.

Requirements: NVDA 2025.1+, Windows x64, and built-in Remote Access on both
computers. Computer audio excluding NVDA requires Windows build 20348+
(including Windows 11). Exactly two participants are supported. Use headphones
for calls; acoustic echo cancellation is not included.

Validated: automated session/transport/queue/package tests, Windows compilation,
native Opus mono/stereo tests, local device enumeration and process-loopback
startup/shutdown under a non-administrator token, and executable manifest/DLL
inspection. No microphone was opened in local smoke checks.
Real wxPython menu creation/state changes/teardown also passed using the
toolkit installed with NVDA 2026.2, with NVDA services stubbed.

This is a **preview**. Full interactive two-computer NVDA testing is still
pending. Existing Remote relay servers must forward extension messages; the
relay operator can access audio just as they can access ordinary Remote data.

The SHA-256 checksum is attached as `SHA256SUMS.txt`. The complete source and
manual verification checklist are in the repository.
