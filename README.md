# Remote Audio and Call for NVDA

Two separate audio modes for NVDA's built-in Remote Access. Install the same
add-on on both computers. No administrator access, audio driver, service,
additional server, or separate Python installation is required.

This is an initial preview. Automated tests and local helper checks do not
replace a two-computer test with NVDA; see [validation](docs/VALIDATION.md).

## Install

Download `remoteAudioCall-0.1.0.nvda-addon` from
[Releases](https://github.com/cha0t1cnu3tral/nvda-remote-audio-call/releases).
Open the file, confirm installation in NVDA, and restart NVDA. Install it on
both computers. Use a user-writable NVDA configuration or a portable NVDA copy
if your organization's installation is managed.

Requires NVDA 2025.1 or newer, Windows x64, and built-in Remote Access.
Computer audio excluding NVDA requires Windows build 20348 or later (Windows
11 satisfies this requirement). On older Windows builds Call still works;
computer audio sharing is disabled rather than including NVDA speech.
Windows microphone privacy settings must allow desktop apps to use your mic.
The add-on cannot override organization policies.

## Connect

Connect normally using **NVDA → Tools → Remote Access → Connect**. Choose
**Control another machine** on one computer and **Allow this machine to be
controlled** on the other. Both use the same server and key.

This preview supports exactly two computers: one controller and one controlled
computer. Additional participants stop audio. Everyone sharing a Remote key
must be trusted: the relay carries audio within that existing Remote channel.
TLS protects the connection to the relay; this is not end-to-end encryption
against the relay operator. Some relay servers may filter extension messages;
both ends must support forwarding custom protocol messages.

## Call mode

1. Open **NVDA → Tools → Remote Audio and Call → Start call** on either computer.
2. The other computer rings and presents **Answer** and **Decline**. Those
   actions are also available in the Tools submenu.
3. Answer to begin a two-way microphone conversation. Microphones remain off
   until the call is answered.
4. Use **Mute microphone** to mute your microphone and **Stop audio or hang up**
   to end the call.

Unanswered calls time out after 30 seconds. Use headphones: this version does
not provide acoustic echo cancellation. Computer audio is off during calls.

## Audio mode

On the **controlled computer**, choose **Tools → Remote Audio and Call → Share
computer audio (excluding NVDA)**. The controller automatically hears the
computer's applications. NVDA's own process and child speech processes are
excluded; normal Remote speech continues independently. Capture covers
applications across output devices, not just one selected speaker.

Either person can stop the stream. Starting a call ends computer audio;
starting computer audio ends a call. Modes never mix, and stopped modes never
resume automatically. Audio is not recorded to files.

NVDA speech rendered by an external application outside NVDA's process tree
cannot be identified automatically and may be included. Protected media and
applications that bypass Windows' shared audio engine may not be captured.

## Settings and status

The Tools submenu includes **Settings** for microphone, playback device, and
listening volume, and **Report status**. Device changes apply to the next
stream; volume changes apply immediately. Windows default devices are selected
initially. Settings live in `remoteAudioCall.json` in your NVDA configuration.

Optional keyboard shortcuts can be assigned in **Preferences → Input gestures →
Remote Audio and Call**. Default shortcuts are not assigned.

Audio stops when the connection ends, either computer locks, the helper fails,
or NVDA exits. Unlocking/reconnecting requires starting the mode again. Secure
desktops, login screens, and elevation prompts do not carry audio.

## Troubleshooting

- **Controls unavailable:** connect exactly one computer in each role, install
  the add-on on both, and wait a few seconds for capability negotiation.
- **Waiting for the other add-on:** check both installations and whether the
  relay forwards custom messages.
- **Microphone error:** check Windows desktop-app microphone permissions and
  choose an available microphone in Settings.
- **Device unplugged:** the stream stops. Select an available device and restart.
- **Sound delayed:** the existing Remote TCP connection carries audio. Poor
  networks can delay it; queues are bounded to avoid growing backlogs. This
  preview is not a replacement for a dedicated voice service.

## Build from source

Use Windows x64, Python 3.11+, CMake 3.24+, Visual Studio 2022 C++ build tools,
and a Windows SDK containing process-loopback APIs. These are developer tools;
people installing the packaged add-on do not need them.

```powershell
python -m unittest discover -s tests -v
cmake -S helper -B build -A x64
cmake --build build --config Release --parallel
./build/Release/remoteAudioHelper.exe --self-test
python scripts/package.py --helper build/Release/remoteAudioHelper.exe
```

Output: `dist/remoteAudioCall-0.1.0.nvda-addon` and `dist/SHA256SUMS.txt`.
GitHub Actions builds the same archive on each push. Opus 1.6.1 is downloaded
with a pinned SHA-256 and linked statically, along with the C++ runtime. The
helper uses an `asInvoker` manifest and communicates through anonymous pipes.
See [protocol details](docs/PROTOCOL.md) and [third-party notices](THIRD_PARTY_NOTICES.md).

