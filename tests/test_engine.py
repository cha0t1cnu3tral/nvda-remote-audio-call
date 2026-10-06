import base64
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / "addon/globalPlugins/remoteAudioCall/engine.py"
spec = importlib.util.spec_from_file_location("audio_engine", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Backend:
    def __init__(self, wire, identity):
        self.wire, self.identity = wire, identity
        self.permitted = True
        self.supported = True
        self.notifications = []
        self.helpers = []
        self.played = []
        self.mutes = []
        self.pending_ready = None
        self.auto_ready = True
        self.invitations = 0
        self.stops = 0

    def allowed(self): return self.permitted
    def supports_system_audio(self): return self.supported
    def send(self, data): self.wire.append((self.identity, data))
    def changed(self): pass
    def notify(self, data): self.notifications.append(data)
    def incoming_call(self): self.invitations += 1
    def stop_audio(self): self.stops += 1
    def start_audio(self, kind, ready, packet, error):
        self.helpers.append((kind, ready, packet, error))
        self.pending_ready = ready
        if self.auto_ready: ready()
    def play(self, packet): self.played.append(packet)
    def mute(self, muted): self.mutes.append(muted)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.wire = []
        self.now = 100
        self.a = Backend(self.wire, 1)
        self.b = Backend(self.wire, 2)
        self.controller = module.Engine(self.a, lambda: self.now)
        self.controlled = module.Engine(self.b, lambda: self.now)
        self.controller.connect("controlling", 2)
        self.controlled.connect("controlled", 1)
        self.flush()

    def flush(self):
        count = 0
        while self.wire:
            origin, msg = self.wire.pop(0)
            receiver = self.controlled if origin == 1 else self.controller
            if msg["action"] == "frame": receiver.receive_frame(msg, origin)
            else: receiver.receive(msg, origin)
            count += 1
            self.assertLess(count, 100)

    def call(self):
        self.controller.start_call()
        self.flush()
        self.controlled.answer()
        self.flush()
        self.assertEqual((self.controller.state, self.controlled.state), ("call", "call"))

    def audio(self):
        self.controlled.start_system_audio()
        self.flush()
        self.assertEqual((self.controller.state, self.controlled.state), ("audio", "audio"))

    def test_capability_handshake(self):
        self.assertTrue(self.controller.available)
        self.assertTrue(self.controlled.available)
        self.assertTrue(self.controller.peer_system_audio)

    def test_call_does_not_open_microphones_until_answer(self):
        self.controller.start_call()
        self.flush()
        self.assertEqual(self.controlled.state, "incoming_call")
        self.assertFalse(self.a.helpers)
        self.assertFalse(self.b.helpers)
        self.controlled.answer()
        self.flush()
        self.assertEqual(self.a.helpers[0][0], "call")
        self.assertEqual(self.b.helpers[0][0], "call")
        self.assertEqual(self.controller.state, "call")

    def test_call_in_either_direction(self):
        self.controlled.start_call()
        self.flush()
        self.controller.answer()
        self.flush()
        self.assertEqual(self.controlled.state, "call")

    def test_decline_leaves_both_off(self):
        self.controller.start_call()
        self.flush()
        self.controlled.decline()
        self.flush()
        self.assertEqual((self.controller.state, self.controlled.state), ("idle", "idle"))
        self.assertFalse(self.a.helpers)
        self.assertFalse(self.b.helpers)

    def test_timeout_clears_remote_invitation(self):
        self.controller.start_call()
        self.flush()
        self.now += 31
        self.controller.tick()
        self.flush()
        self.assertEqual(self.controlled.state, "idle")
        self.assertIn("Call unanswered", self.a.notifications)

    def test_simultaneous_calls_converge(self):
        self.controller.start_call()
        self.controlled.start_call()
        self.flush()
        states = {self.controller.state, self.controlled.state}
        self.assertEqual(states, {"incoming_call", "outgoing_call"})
        answering = self.controller if self.controller.state == "incoming_call" else self.controlled
        answering.answer()
        self.flush()
        self.assertEqual((self.controller.state, self.controlled.state), ("call", "call"))

    def test_audio_only_controlled_side_can_start(self):
        self.controller.start_system_audio()
        self.assertEqual(self.controller.state, "idle")
        self.assertFalse(self.wire)
        self.audio()
        self.assertEqual(self.a.helpers[0][0], "receive")
        self.assertEqual(self.b.helpers[0][0], "send")

    def test_unsupported_system_audio_never_falls_back(self):
        self.b.supported = False
        self.controlled.start_system_audio()
        self.assertFalse(self.b.helpers)
        self.assertEqual(self.controlled.state, "idle")

    def test_audio_then_call_stops_system_stream(self):
        self.audio()
        stops = self.b.stops
        self.controller.start_call()
        self.flush()
        self.assertGreater(self.b.stops, stops)
        self.assertEqual(self.controlled.state, "incoming_call")
        self.controlled.answer()
        self.flush()
        self.assertEqual(self.controller.state, "call")

    def test_call_then_audio_disables_microphones(self):
        self.call()
        self.audio()
        self.assertEqual(self.b.helpers[-1][0], "send")
        self.assertEqual(self.a.helpers[-1][0], "receive")

    def test_hangup_and_controller_stop_audio(self):
        self.call()
        self.controlled.stop()
        self.flush()
        self.assertEqual(self.controller.state, "idle")
        self.audio()
        self.controller.stop()
        self.flush()
        self.assertEqual(self.controlled.state, "idle")

    def test_mute_is_local_and_reset(self):
        self.call()
        self.controller.toggle_mute()
        self.assertEqual(self.a.mutes, [True])
        self.assertFalse(self.controlled.muted)
        self.controller.toggle_mute()
        self.assertEqual(self.a.mutes, [True, False])
        self.controller.stop()
        self.assertFalse(self.controller.muted)

    def test_frame_direction_and_full_duplex(self):
        self.audio()
        self.b.helpers[-1][2](b"sound")
        self.a.helpers[-1][2](b"wrong direction")
        self.flush()
        self.assertEqual(self.a.played, [b"sound"])
        self.assertEqual(self.b.played, [])
        self.call()
        self.a.helpers[-1][2](b"voice1")
        self.b.helpers[-1][2](b"voice2")
        self.flush()
        self.assertEqual(self.a.played[-1], b"voice2")
        self.assertEqual(self.b.played[-1], b"voice1")

    def frame(self, **overrides):
        return dict(protocol=1, token=self.controller.token, sequence=1,
                    data=base64.b64encode(b"audio").decode(), **overrides)

    def test_reject_malformed_duplicate_stale_or_wrong_sender_frames(self):
        self.call()
        valid = self.frame()
        self.controller.receive_frame(valid, 99)
        cases = [("data", "%%%"), ("data", "A" * 1701), ("sequence", True),
                 ("sequence", -1), ("sequence", 2**53), ("protocol", 2),
                 ("token", "x" * 32)]
        for key, value in cases:
            bad = dict(valid, **{key: value})
            self.controller.receive_frame(bad, 2)
        self.assertFalse(self.a.played)
        self.controller.receive_frame(valid, 2)
        self.controller.receive_frame(valid, 2)
        self.assertEqual(self.a.played, [b"audio"])

    def test_old_helper_callbacks_cannot_restart_or_send(self):
        self.call()
        old = self.a.helpers[-1]
        self.controller.stop()
        self.flush()
        old[1]()
        old[2](b"late")
        old[3]("late failure")
        self.assertEqual(self.controller.state, "idle")
        self.assertFalse(self.wire)

    def test_error_ends_both_sides(self):
        self.call()
        self.b.helpers[-1][3]("microphone disconnected")
        self.flush()
        self.assertEqual((self.controller.state, self.controlled.state), ("idle", "idle"))
        self.assertIn("Audio stopped: microphone disconnected", self.b.notifications)

    def test_permission_and_disconnect_never_resume(self):
        self.call()
        self.b.permitted = False
        self.b.helpers[-1][2](b"blocked")
        self.assertFalse(self.wire)
        self.controlled.disconnect()
        self.b.permitted = True
        self.controlled.connect("controlled", 1)
        self.flush()
        self.assertEqual(self.controlled.state, "idle")

    def test_peer_restart_cancels_active_stream(self):
        self.call()
        self.controller.receive(dict(protocol=1, action="hello", instance="f"*32, system_audio=True), 2)
        self.assertEqual(self.controller.state, "idle")

    def test_helper_setup_timeout(self):
        self.a.auto_ready = False
        self.controlled.start_system_audio()
        self.flush()
        self.assertEqual(self.controller.state, "preparing_receiver")
        self.now += 16
        self.controller.tick()
        self.flush()
        self.assertEqual((self.controller.state, self.controlled.state), ("idle", "idle"))

    def test_unsupported_protocol_does_not_start_capture(self):
        self.controlled.receive(dict(protocol=2, action="call_offer", token="f"*32), 1)
        self.assertEqual(self.controlled.state, "idle")
        self.assertFalse(self.b.helpers)


if __name__ == "__main__":
    unittest.main()

