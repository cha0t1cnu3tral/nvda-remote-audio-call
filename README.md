# Remote Audio and Call for NVDA

Voice calls and computer audio sharing for NVDA's built-in Remote Access. Install the same
add-on on both computers. No administrator access, audio driver, service,
additional server, or separate Python installation is required.

This is an initial preview. Automated tests and local helper checks do not
replace a two-computer test with NVDA; see [validation](docs/VALIDATION.md).

## Install

Download `remoteAudioCall-0.1.4.nvda-addon` from
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

Calls and computer audio use one selected controller/controlled pair. Extra
computers can remain connected, including computers without this add-on. They
do not disable the selected pair or become audio candidates. Both audio
endpoints must install 0.1.3 or newer; earlier releases use a different protocol.

After a short discovery period, the sole compatible opposite-role computer is
selected automatically. With multiple compatible computers, choose **Tools >
Remote Audio and Call > Choose remote computer**. The accessible list shows
the Remote client ID and role. Selecting a different computer stops current
audio. Selection lasts for this Remote session. An addressed incoming call
can select its caller while idle; invitations from others are declined while
audio is active.

Everyone sharing a Remote key must be trusted: the relay carries audio within
that existing Remote channel. Recipient checks prevent unintended playback or
ringing in the add-on; a broadcast relay can still deliver packets to observers.
TLS protects the connection to the relay; this is not end-to-end encryption
against the relay operator. Some relay servers may filter extension messages;
both ends must support forwarding custom protocol messages.

## Call mode

Choose **Tools → Remote Audio and Call → Choose microphone and speakers** on
each computer before calling. Select the microphone and headphones/speakers
you actually use, then save. Device changes apply to the next call. The
incoming-call window also has a **Microphone and speakers** button.

1. Open **NVDA → Tools → Remote Audio and Call → Start call** on either computer.
2. The other computer rings and presents **Answer** and **Decline**. Those
   actions are also available in the Tools submenu.
3. Answer to begin a two-way microphone conversation. Microphones remain off
   until the call is answered.
4. Use **Mute microphone** to mute your microphone and **Stop audio or hang up**
   to end the call.

Unanswered calls time out after 30 seconds. Use headphones: this version does
not provide acoustic echo cancellation. Computer audio can play during calls.

## Audio mode

On the **controlled computer**, choose **Tools → Remote Audio and Call → Share
computer audio (excluding NVDA)**. The controller automatically hears the
computer's applications. NVDA's own process and child speech processes are
excluded; normal Remote speech continues independently. Capture covers
applications across output devices, not just one selected speaker.

Computer audio and a voice call can run together. Start sharing on the controlled
computer before or during a call; answering or declining a call leaves the shared
audio running. Microphone mute affects only your voice. **Stop audio or hang up**
stops both streams. Both computers need version 0.1.3 or newer for simultaneous
audio. Audio is not recorded to files.

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

With 0.1.4 on both computers, an answered call can recover automatically after
a brief Remote Access outage. Capture and playback stop immediately while
disconnected. The add-on waits up to 30 seconds for the same computer and the
same running add-on instance to reconnect, then confirms that both sides still
have the accepted call before restarting audio. Microphone mute is preserved.
**Stop audio or hang up** also cancels pending recovery. Report status explains
whether the call is reconnecting or recovering its audio.

An audio-helper failure during an established call triggers up to three restart
attempts, with delays of one, two, and four seconds. Repeated failures end the
call with an explanation. Computer audio is independent and continues through
a local call-helper failure; restart computer audio sharing after a network outage.
Locking either computer, changing peers, ending the Remote session, restarting
NVDA, or exceeding the recovery window requires a new call. Secure desktops,
login screens, and elevation prompts do not carry audio.

## Troubleshooting

- **Starting a call or audio share:** connect through Remote Access and use
  **Choose remote computer** if several audio partners are available. Extra
  computers without the add-on do not block audio. **Report status** gives
  the selected client ID or explains why no compatible peer is available.
  Start call remains usable when disconnected and explains the missing connection.
- **Waiting for the other add-on:** check both installations and whether the
  relay forwards custom messages.
- **Microphone error:** check Windows desktop-app microphone permissions and
  choose an available microphone in Settings.
- **Call connects without sound:** use **Choose microphone and speakers** on
  both computers and restart the call. **Report status** distinguishes missing
  microphone frames, no received audio, and playback that has not started.
  The Windows communications default can differ from your usual sound device.
- **Device unplugged:** an established call attempts to restart its audio helper.
  Reconnect the device promptly. If recovery fails, select an available device
  and start a new call. Mute remains in effect during recovery.
- **Connection drops:** keep Remote Access connected and allow its reconnection
  to finish. With 0.1.4 on both ends, the accepted call recovers within the
  30-second window. Longer outages require a new call. A sole compatible peer
  is selected directly when you start a call; multiple peers still show a chooser.
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

Output: `dist/remoteAudioCall-0.1.4.nvda-addon` and `dist/SHA256SUMS.txt`.
GitHub Actions builds the same archive on each push. Opus 1.6.1 is downloaded
with a pinned SHA-256 and linked statically, along with the C++ runtime. The
helper uses an `asInvoker` manifest and communicates through anonymous pipes.
See [protocol details](docs/PROTOCOL.md) and [third-party notices](THIRD_PARTY_NOTICES.md).
