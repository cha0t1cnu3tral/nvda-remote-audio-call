# Validation

Automated checks cover the session state machine, packet validation, queue
bounds, and packaging. Windows CI compiles the helper and runs mono/stereo
Opus encode/decode checks. An `asInvoker` manifest and static runtime eliminate
elevation/runtime installation requirements.

Before treating this preview as stable, perform these checks on two computers:

- Install the release archive and restart NVDA on both.
- Navigate every menu item and dialog using keyboard and NVDA speech.
- Call from each side; Answer, Decline, leave unanswered, invite simultaneously,
  mute/unmute, and hang up. Verify no microphone audio before acceptance.
- Share sound from the controlled side. Play music and speak with NVDA:
  music reaches the controller and NVDA is excluded from the audio stream.
- Start computer audio before and during a call; verify both remain audible.
  Microphone mute must leave computer audio playing; Stop must end both.
- Stop listening from the controller and restart only from the controlled side.
- Disconnect, reconnect, lock/unlock, close NVDA, unplug microphones/playback,
  and forcibly stop the helper. Audio must stop and never resume automatically.
- Test unavailable microphones and disabled Windows microphone privacy access.
- Join a third participant: audio must stop.
- Test slow networks while continuing keyboard control and normal Remote speech.
- Run on a standard user account; confirm no UAC prompt, service, driver,
  firewall rule, or extra audio server is required.

The automated tests are not evidence that these interactive two-computer checks
passed. See GitHub release notes for the checks actually completed for a build.

## 0.1.2 checks completed

- 50 automated tests cover independent concurrent streams, failure isolation,
  immediate device shutdown, same-transport reconnects, stale participant IDs,
  stale event callbacks, and restored menu availability.
- Actual NVDA 2026.2 wxPython checks pass simultaneous-stream menu state and
  recovery after reconnecting with stale participant IDs, with NVDA services
  stubbed and windows hidden.
- Public TLS relay checks pass simultaneous synthetic voice and stereo audio
  packets, calls from both sides, and stopping both streams. No microphone is
  opened in this check.
- The unchanged native helper passes its mono/stereo codec self-test, device
  enumeration, and executable manifest/dependency inspection.
- Native duplex calls also pass through the public TLS relay in both call
  directions, with microphones muted from startup and playback volume zero.
- Audible simultaneous music and conversation on two physical NVDA computers
  remain to be verified.

## 0.1.1 call checks completed

- 38 automated tests passed, including answering near the ring timeout,
  discovery of both built-in Remote roles and UI-thread disconnect cleanup.
- `python scripts/smoke_relay.py --server nvdaremote.com` passed capability
  negotiation, calls initiated in both directions, duplex synthetic packets
  and hang-up through TLS in a random private room. No microphone was opened
  by this test, and no user's existing Remote channel was joined.
- Adding `--native-helper <helper.exe>` passed the full native duplex pipeline
  over the same TLS relay in both call directions. Both helpers captured and
  encoded frames, exchanged them through the actual adapter, decoded received
  packets, and consumed their samples in the WASAPI playback buffers. Both
  microphones were muted before capture started; playback volume was zero.
  This checks the complete data path without sending microphone speech.
- `python scripts/smoke_helper.py <helper.exe> --call --loopback` passed local
  microphone/playback initialization, mono Opus capture and clean shutdown,
  as well as the earlier native checks, under a non-administrator account.
  Microphone packets were discarded in memory and never sent to a network.
- The real NVDA 2026.2 wxPython smoke check now also builds a hidden incoming
  call dialog, verifies Answer destroys it and enters the waiting state, and
  checks saving selected microphone/speaker IDs from the device picker.
- Full interactive spoken calls between two NVDA computers remain pending.

## Initial preview checks completed

- 33 automated tests passed locally, covering session negotiation, separate
  modes, invitations, mute, stale/malformed frames, transport hooks, lock
  cleanup, queue bounds and reproducible packaging.
- Windows CI successfully compiled the x64 helper and checked Opus mono/stereo
  encode/decode.
- The compiled helper ran locally under a non-administrator token, enumerated
  audio devices, and started/stopped process-loopback capture excluding a
  target process tree. Packets were discarded; no microphone was opened.
- Executable inspection confirmed an `asInvoker` manifest and imports only
  from Windows DLLs (no separate VC++ runtime or Opus DLL).
- A separate Python 3.13 process loaded the wxPython shipped with NVDA 2026.2
  and verified actual Tools menu creation, state changes, event bindings, and
  teardown with NVDA services stubbed. No windows were shown and the running
  NVDA profile was untouched.
- Python files compile successfully. Full interactive testing with two NVDA
  computers is still pending.
