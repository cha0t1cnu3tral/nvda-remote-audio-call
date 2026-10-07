"""Audio session state machine; independent of NVDA and Windows for testing."""
import base64
import binascii
import functools
import threading
import time
import uuid

PROTOCOL = 1
MESSAGE = "remote_audio_call_v1"
MAX_PACKET = 1275


def synchronized(method):
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


class StreamEngine:
    def __init__(self, backend, clock=time.monotonic):
        self._lock = threading.RLock()
        self.backend = backend
        self.clock = clock
        self.instance = uuid.uuid4().hex
        self.peer_instance = None
        self.role = None
        self.peer = None
        self.capable = False
        self.peer_system_audio = False
        self.state = "idle"
        self.token = None
        self.deadline = 0
        self.sequence = 0
        self.last_sequence = -1
        self.muted = False

    @property
    @synchronized
    def available(self):
        return self.peer is not None and self.capable and self.backend.allowed()

    @synchronized
    def connect(self, role, peer):
        self.disconnect()
        self.role, self.peer = role, peer
        self.hello()

    @synchronized
    def disconnect(self):
        self.stop(send=False, announce=False)
        self.role = self.peer = self.peer_instance = None
        self.capable = False
        self.peer_system_audio = False
        self.backend.changed()

    @synchronized
    def hello(self):
        if self.peer is not None:
            self.send("hello", instance=self.instance, system_audio=self.backend.supports_system_audio(), simultaneous=True)

    @synchronized
    def send(self, action, **payload):
        if self.peer is not None:
            self.backend.send(dict(action=action, protocol=PROTOCOL, target=self.peer,
                                   token=self.token, **payload))

    def _new(self, state, token=None, timeout=15):
        self.token = token or uuid.uuid4().hex
        self.state = state
        self.deadline = self.clock() + timeout
        self.sequence = 0
        self.last_sequence = -1
        self.muted = False
        self.backend.changed()

    @synchronized
    def stop(self, send=True, announce=True):
        active = self.state != "idle"
        if active and send:
            self.send("stop")
        self.state = "idle"
        self.token = None
        self.deadline = 0
        self.muted = False
        self.backend.stop_audio()
        self.backend.changed()
        if active and announce:
            self.backend.notify("Audio and call stopped")

    def _require_peer(self):
        if not self.available:
            self.backend.notify("Connect one controlling and one controlled computer, both with Remote Audio and Call installed and unlocked")
            return False
        return True

    @synchronized
    def start_call(self):
        if not self._require_peer():
            return
        self.stop(announce=False)
        self._new("outgoing_call", timeout=30)
        self.send("call_offer")
        self.backend.notify("Calling. Waiting for an answer")

    @synchronized
    def answer(self):
        if self.state != "incoming_call" or not self.available:
            return
        self.state = "answering_call"
        self.deadline = self.clock() + 15
        self.backend.changed()
        # Device initialization can outlast the invitation's remaining time.
        self.send("call_answering")
        self._start_helper("call")

    @synchronized
    def decline(self):
        if self.state == "incoming_call":
            self.send("decline")
            self.stop(send=False, announce=False)
            self.backend.notify("Call declined")

    @synchronized
    def start_system_audio(self):
        if not self._require_peer():
            return
        if self.role != "controlled":
            self.backend.notify("Computer audio must be started on the controlled computer")
            return
        if not self.backend.supports_system_audio():
            self.backend.notify("Sharing audio without NVDA speech requires Windows build 20348 or later")
            return
        self.stop(announce=False)
        self._new("outgoing_audio")
        self.send("audio_offer")
        self.backend.notify("Starting computer audio sharing")

    @synchronized
    def toggle_mute(self):
        if self.state != "call":
            return
        self.muted = not self.muted
        self.backend.mute(self.muted)
        self.backend.changed()
        self.backend.notify("Microphone muted" if self.muted else "Microphone unmuted")

    def _start_helper(self, kind):
        token = self.token
        self.backend.start_audio(kind,
            lambda: self._ready(token),
            lambda packet: self._packet(token, packet),
            lambda error: self._error(token, error))

    @synchronized
    def _ready(self, token):
        if token != self.token or not self.available:
            return
        if self.state == "answering_call":
            self.state = "waiting_call"
            self.send("call_accept")
        elif self.state == "connecting_call":
            self.state = "call"
            self.deadline = 0
            self.send("call_ready")
            self.backend.notify("Call connected")
        elif self.state == "preparing_receiver":
            self.state = "waiting_audio"
            self.send("audio_ready")
        elif self.state == "preparing_sender":
            self.state = "audio"
            self.deadline = 0
            self.send("audio_started")
            self.backend.notify("Computer audio sharing started. NVDA speech is excluded")
        self.backend.changed()

    @synchronized
    def _packet(self, token, packet):
        # Called from the helper reader thread. State/token checks prevent late sends.
        if token != self.token or not self.available:
            return
        if self.state != "call" and not (self.state == "audio" and self.role == "controlled"):
            return
        if not 0 < len(packet) <= MAX_PACKET:
            return
        self.sequence += 1
        self.send("frame", sequence=self.sequence, data=base64.b64encode(packet).decode("ascii"))

    @synchronized
    def _error(self, token, error):
        if token != self.token:
            return
        self.stop(announce=False)
        self.backend.notify("Audio stopped: " + str(error)[:240])

    @synchronized
    def receive(self, message, origin):
        if origin != self.peer or not isinstance(message, dict):
            return
        if message.get("protocol") != PROTOCOL:
            return
        action = message.get("action")
        if action in ("hello", "hello_ack"):
            instance = message.get("instance")
            if not isinstance(instance, str) or len(instance) != 32:
                return
            if self.peer_instance and instance != self.peer_instance:
                self.stop(send=False, announce=False)
            self.peer_instance = instance
            self.capable = True
            self.peer_system_audio = message.get("system_audio") is True
            if action == "hello":
                self.send("hello_ack", instance=self.instance, system_audio=self.backend.supports_system_audio(), simultaneous=True)
            self.backend.changed()
            return
        if not self.available:
            return
        token = message.get("token")
        if not isinstance(token, str) or len(token) != 32:
            return
        if action == "call_offer":
            if self.state == "outgoing_call":
                # Simultaneous invitations converge on the smaller token.
                if token >= self.token:
                    self.backend.send(dict(action="decline", protocol=PROTOCOL, target=self.peer, token=token))
                    return
            elif self.state not in ("idle", "audio"):
                self.backend.send(dict(action="decline", protocol=PROTOCOL, target=self.peer, token=token))
                return
            self.stop(announce=False)
            self._new("incoming_call", token, timeout=30)
            self.backend.incoming_call()
            return
        if action == "audio_offer":
            if self.role != "controlling" or not self.peer_system_audio or self.state != "idle":
                self.backend.send(dict(action="decline", protocol=PROTOCOL, target=self.peer, token=token))
                return
            self._new("preparing_receiver", token)
            self._start_helper("receive")
            return
        if token != self.token:
            return
        if action == "call_answering" and self.state == "outgoing_call":
            self.deadline = self.clock() + 15
        elif action == "call_accept" and self.state == "outgoing_call":
            self.state = "connecting_call"
            self.deadline = self.clock() + 15
            self.backend.changed()
            self._start_helper("call")
        elif action == "call_ready" and self.state == "waiting_call":
            self.state = "call"
            self.deadline = 0
            self.backend.notify("Call connected")
            self.backend.changed()
        elif action == "audio_ready" and self.state == "outgoing_audio":
            self.state = "preparing_sender"
            self.deadline = self.clock() + 15
            self._start_helper("send")
        elif action == "audio_started" and self.state == "waiting_audio":
            self.state = "audio"
            self.deadline = 0
            self.backend.notify("Listening to the controlled computer")
            self.backend.changed()
        elif action in ("stop", "decline"):
            self.stop(send=False, announce=False)
            self.backend.notify("Call declined or audio unavailable" if action == "decline" else "Audio and call ended")

    @synchronized
    def receive_frame(self, message, origin):
        # Network thread: decode only bounded packets, never queue work on the UI thread.
        if origin != self.peer or not self.available or message.get("token") != self.token:
            return
        if self.state != "call" and not (self.state == "audio" and self.role == "controlling"):
            return
        if message.get("protocol") != PROTOCOL:
            return
        seq, data = message.get("sequence"), message.get("data")
        if type(seq) is not int or not 0 < seq < 2**53 or seq <= self.last_sequence:
            return
        if not isinstance(data, str) or not 0 < len(data) <= 1700:
            return
        try:
            packet = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            return
        if not 0 < len(packet) <= MAX_PACKET:
            return
        self.last_sequence = seq
        self.backend.play(packet)

    @synchronized
    def tick(self):
        if self.deadline and self.clock() >= self.deadline:
            outgoing_call = self.state == "outgoing_call"
            self.stop(announce=False)
            self.backend.notify("Call unanswered" if outgoing_call else "Audio or call request timed out")


class StreamBackend:
    """Give each stream its own helper and tag its messages on the wire."""
    def __init__(self, backend, stream):
        self.backend, self.stream = backend, stream

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def send(self, payload):
        self.backend.send(dict(payload, stream=self.stream))

    def start_audio(self, kind, ready, packet, error):
        self.backend.start_audio(kind, ready, packet, error, stream=self.stream)

    def stop_audio(self):
        self.backend.stop_audio(stream=self.stream)

    def play(self, packet):
        self.backend.play(packet, stream=self.stream)


class Engine:
    """Independent voice and computer-audio sessions over one Remote connection."""
    def __init__(self, backend, clock=time.monotonic):
        self.backend = backend
        self.call = StreamEngine(StreamBackend(backend, "call"), clock)
        self.audio = StreamEngine(StreamBackend(backend, "audio"), clock)
        self.audio.instance = self.call.instance
        self.simultaneous = False

    def __getattr__(self, name):
        return getattr(self.call, name)

    @property
    def state(self):
        return self.call.state if self.call.state != "idle" else self.audio.state

    @state.setter
    def state(self, value):
        self.call.state = value

    @property
    def token(self):
        return self.call.token if self.call.state != "idle" else self.audio.token

    @token.setter
    def token(self, value):
        self.call.token = value

    @property
    def capable(self):
        return self.call.capable

    @capable.setter
    def capable(self, value):
        self.call.capable = self.audio.capable = value

    def connect(self, role, peer):
        self.disconnect()
        self.call.role = self.audio.role = role
        self.call.peer = self.audio.peer = peer
        self.hello()

    def disconnect(self):
        self.call.disconnect()
        self.audio.disconnect()
        self.simultaneous = False

    def stop(self, send=True, announce=True):
        active = self.state != "idle"
        self.call.stop(send=send, announce=False)
        self.audio.stop(send=send, announce=False)
        if active and announce:
            self.backend.notify("Audio and call stopped")

    def start_call(self):
        if self.audio.state != "idle" and not self.simultaneous:
            self.audio.stop(announce=False)
        self.call.start_call()

    def start_system_audio(self):
        if self.call.state != "idle" and not self.simultaneous:
            self.backend.notify("Install version 0.1.2 or newer on both computers to share computer audio during a call")
            return
        self.audio.start_system_audio()

    def receive(self, message, origin):
        if not isinstance(message, dict):
            return
        if origin != self.call.peer or message.get("protocol") != PROTOCOL:
            return
        action = message.get("action")
        if action in ("hello", "hello_ack"):
            instance = message.get("instance")
            if not isinstance(instance, str) or len(instance) != 32:
                return
            # A single handshake establishes capability for both streams.
            self.call.receive(message, origin)
            self.audio.receive(dict(message, action="hello_ack"), origin)
            self.simultaneous = message.get("simultaneous") is True
            return
        stream = self._stream(message)
        if action == "call_offer" and self.audio.state != "idle" and not self.simultaneous:
            self.audio.stop(announce=False)
        stream.receive(message, origin)

    def _stream(self, message):
        if message.get("stream") == "audio" or str(message.get("action", "")).startswith("audio_"):
            return self.audio
        if message.get("stream") is None and self.audio.token is not None and message.get("token") == self.audio.token:
            return self.audio
        return self.call

    def receive_frame(self, message, origin):
        self._stream(message).receive_frame(message, origin)

    def tick(self):
        self.call.tick()
        self.audio.tick()
