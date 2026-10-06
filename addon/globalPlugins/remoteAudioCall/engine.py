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


class Engine:
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

    @synchronized
    def hello(self):
        if self.peer is not None:
            self.send("hello", instance=self.instance, system_audio=self.backend.supports_system_audio())

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
                self.send("hello_ack", instance=self.instance, system_audio=self.backend.supports_system_audio())
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
        if action == "call_accept" and self.state == "outgoing_call":
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
