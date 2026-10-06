# Protocol and lifecycle

Extension message type: `remote_audio_call_v1`. Messages travel as newline
JSON on the existing Remote transport and use protocol version 1. The relay
supplies `origin`. Outgoing messages include a target peer ID; v1 is restricted
to two participants, so receipt is gated by the unique expected origin.

`hello` / `hello_ack` negotiate protocol capability, process-instance nonce,
and system-audio availability. A changed instance cancels existing streams.
Only the opposite-role peer in a two-client session is accepted.

Call flow: `call_offer` → user Answer → callee helper ready → `call_accept` →
caller helper ready → `call_ready`. Both sides can then exchange frames.
Invitations time out at 30 seconds; device setup at 15 seconds. Simultaneous
invitations converge on the lexicographically smaller invitation token.
`decline` or `stop` ends the matching stream.

Audio flow: controlled user starts → `audio_offer` → controller helper ready →
`audio_ready` → controlled helper ready → `audio_started`. Frames then travel
only from controlled to controller. Only one stream is active at a time.

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
(helper to plugin). Closing/killing the child tears down audio devices. No
audio files, listening sockets, elevated process, or persistent service exist.
Capture and playback workers check the active input desktop at least every
50 ms and stop on a secure or unavailable desktop, including UAC transitions.
