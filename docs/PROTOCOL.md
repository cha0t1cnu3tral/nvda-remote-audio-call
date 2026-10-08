# Protocol and lifecycle

Extension message type: `remote_audio_call_v2`, protocol version 2, introduced
in 0.1.3. Messages travel as newline JSON on the existing Remote transport.
Both audio endpoints must upgrade; v1 peers are not audio candidates.

Discovery broadcasts `hello` independently of selection. Opposite-role peers
are identified from relay membership and advertise their process instance,
system-audio capability and simultaneous-stream capability. `hello_ack` is
addressed with `target_instance`. A two-second initial discovery window avoids
selecting the first responder when multiple compatible peers are present.

Session messages carry `target` (Remote client ID) and `target_instance`
(recipient process nonce). Every recipient checks its own instance before
acting; selected relay origin, stream token and sequence checks also apply.
Relays may broadcast extension packets: addressing prevents unintended
ringing/playback but does not encrypt audio against other channel members.
Additional participants, including those without the add-on, do not invalidate
the selected pair. Ordinary Remote messages always reach NVDA's parser.

`hello` / `hello_ack` negotiate protocol capability, process-instance nonce,
system-audio availability, and `simultaneous: true` for independent streams.
A changed instance cancels both existing streams. Each message includes
`stream: "call"` or `stream: "audio"`; helpers, tokens and sequence counters are
independent. Audio actions/tokens also identify their stream.
Concurrent streams require both peers to advertise simultaneous support.
Only the selected opposite-role peer can exchange media. While idle, an
addressed invitation from another discovered peer selects that sender before
normal invitation handling. Other senders receive a decline while either
stream is active. Malformed or misaddressed invitations cannot change selection.

Call flow: `call_offer` → user Answer → callee helper ready → `call_accept` →
caller helper ready → `call_ready`. Both sides can then exchange frames.
Answer immediately sends `call_answering` to give the caller a fresh 15-second
device-startup timeout, even when the invitation is answered near 30 seconds.
Invitations time out at 30 seconds; device setup at 15 seconds. Simultaneous
invitations converge on the lexicographically smaller invitation token.
`decline` or `stop` ends the matching stream.

Audio flow: controlled user starts → `audio_offer` → controller helper ready →
`audio_ready` → controlled helper ready → `audio_started`. Frames then travel
only from controlled to controller. Calls and computer audio have independent sessions and can run concurrently.

Every stream uses a random 32-character token. `frame` carries this token,
a monotonically increasing sequence, and base64 Opus data. Frames with an
unexpected origin, stale token, repeated sequence, unsupported protocol,
invalid base64, or more than 1275 encoded bytes are rejected. Helpers accept
only 960-sample frames at 48 kHz. Control parsing is limited to 4096 bytes;
media processing stays off NVDA's UI thread.

Audio uses stereo 96 kbps; calls use mono 32 kbps. Each packet represents 20 ms.
Python drops outgoing media when the Remote send queue has six pending items.
The helper IPC queue and native playback queue each hold at most six frames;
older queued playback is discarded when newer frames exceed that bound. TCP
head-of-line blocking and audio-device latency can still add delay.

Anonymous-pipe framing: little-endian 32-bit payload length, then one byte of
kind and the payload. Types: 1 Opus packet, 2 microphone mute (one byte),
3 volume (one byte, 0–100), 4 stop, 5 UTF-8 error (helper to plugin), 6 ready
(helper to plugin).
Helper type 7 carries four little-endian uint32 values: encoded capture
packets, decoded playback packets, samples consumed by the playback buffer,
and the latest microphone peak (0–32768). These local health counters are
not transmitted to the relay. `--mute 1` starts capture muted for diagnostic
tests; the normal call starts unmuted only after user acceptance.
Closing/killing the child tears down audio devices. No audio files, listening
sockets, elevated process, or persistent service exist.
Capture and playback workers check the active input desktop at least every
50 ms and stop on a secure or unavailable desktop, including UAC transitions.

Disconnect events stop both helpers immediately on the network thread, then
reset session/UI state on the UI thread. Handlers bind their source transport
and connection epoch, advanced on network connection/disconnection events.
Membership and control callbacks carry the epoch captured at parse time, so
delayed callbacks cannot reset or modify a newer connection.
The adapter keeps the parser attached while Remote reconnects, and replaces
its participant snapshot from `channel_joined`, then tracks `client_joined` /
`client_left`. Stale IDs in Remote session collections do not block the menus.
Monitoring attaches as soon as a Remote session exists, before connection
completion. Selection remembers the process instance across reconnects of
that session, including a new Remote ID. Ending/replacing the Remote session
clears the preference. No different peer is silently substituted when the
selected one leaves. Reconnection restores controls after discovery; it does not
resume computer audio without a new user action.

Version 0.1.4 adds the optional `call_recovery: true` discovery capability.
For an accepted call only, an unexpected transport outage or selected-peer
departure saves its token, peer instance, local role, mute and sequence counters
for 30 seconds. Helpers still stop immediately. Rediscovery must match the
saved instance and role, and both peers must advertise call recovery.
`call_resume` requests confirmation of the existing token; `call_resumed`
acknowledges it. A peer without that accepted call replies with a matching
`stop`. Only confirmation allows helper restart; no new incoming invitation or
automatic acceptance occurs. Sequence counters continue across the outage.
Explicit transport closing, Stop, lock and plugin detach cancel saved recovery.
Expired recovery, a changed peer instance, and unanswered invitations cannot resume.

Established-call helper failures retry locally after 1, 2 and 4 seconds, with
at most three attempts per 60-second window and 15 seconds per device startup.
Security/desktop failures stop immediately. A helper generation guards all
callbacks so a replaced process cannot send frames or stop its replacement.
Replacement capture starts with the current mute state before any microphone
frames can be captured.
