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
- Switch Call → Audio and Audio → Call without mixed streams.
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

