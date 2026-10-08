"""Contract tests against NVDA/wx stubs, without changing a user's NVDA profile."""
import importlib.util
import json
from pathlib import Path
import queue
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class Action:
    def __init__(self): self.handlers = set()
    def register(self, handler): self.handlers.add(handler)
    def unregister(self, handler): self.handlers.discard(handler)


class Transport:
    def __init__(self):
        self.connected = True
        self.queue = queue.Queue()
        self.parsed = []
        self.transportConnected = Action()
        self.transportDisconnected = Action()
        self.transportClosing = Action()
    def parse(self, line): self.parsed.append(line)


class AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        modules = {}
        for name in ("globalPluginHandler", "globalVars", "gui", "logHandler", "scriptHandler", "tones", "ui", "wx", "utils", "utils.security", "winAPI", "winAPI.sessionTracking"):
            modules[name] = ModuleType(name)
        modules["globalPluginHandler"].GlobalPlugin = object
        modules["logHandler"].log = Mock()
        modules["scriptHandler"].script = lambda **kwargs: (lambda function: function)
        modules["wx"].CallAfter = lambda func, *args: func(*args)
        modules["ui"].message = Mock()
        modules["utils.security"].isRunningOnSecureDesktop = lambda: False
        modules["utils.security"].post_sessionLockStateChanged = Action()
        modules["winAPI.sessionTracking"].isLockScreenModeActive = lambda: False
        cls.modules = modules
        cls.patcher = patch.dict(sys.modules, modules)
        cls.patcher.start()
        root = Path(__file__).resolve().parents[1] / "addon/globalPlugins/remoteAudioCall"
        spec = importlib.util.spec_from_file_location("test_nvda_audio_plugin", root / "__init__.py", submodule_search_locations=[str(root)])
        cls.plugin_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.plugin_module
        spec.loader.exec_module(cls.plugin_module)

    @classmethod
    def tearDownClass(cls):
        cls.patcher.stop()
        for name in list(sys.modules):
            if name.startswith("test_nvda_audio_plugin"):
                del sys.modules[name]

    def setUp(self):
        module = self.plugin_module
        self.plugin = module.GlobalPlugin.__new__(module.GlobalPlugin)
        self.plugin.closed = self.plugin.locked = False
        self.plugin.transport = self.plugin.session = self.plugin.native = None
        self.plugin.old_parse = self.plugin.hooked_parse = self.plugin.pair = None
        self.plugin.menu = None
        self.plugin.audio_native = None
        self.plugin._init_connection_state()
        self.plugin.engine = module.Engine(self.plugin)
        self.plugin.call_dialog = None
        self.plugin.incoming_call = Mock()
        self.plugin.supports_system_audio = lambda: True
        self.transport = Transport()
        self.session = SimpleNamespace(leaders={}, followers={2: {}}, transport=self.transport)
        self.plugin._attach(self.transport, self.session, "controlling")
        self.plugin.discovery_until = 0
        self.plugin._control(self.transport, dict(action="hello", protocol=2, instance="a"*32, simultaneous=True, system_audio=True), 2)
        while not self.transport.queue.empty(): self.transport.queue.get()

    def tearDown(self): self.plugin._detach()

    def test_remote_keyboard_speech_clipboard_messages_are_preserved(self):
        for kind in ("key", "speak", "set_clipboard_text", "tone"):
            raw = json.dumps(dict(type=kind, data="ordinary")).encode()
            self.transport.parse(raw)
            self.assertEqual(self.transport.parsed[-1], raw)

    def test_custom_call_message_does_not_reach_remote_enum_parser(self):
        raw = json.dumps(dict(type=self.plugin_module.MESSAGE, action="call_offer", protocol=2, token="f"*32, target_instance=self.plugin.engine.instance, origin=2)).encode()
        self.transport.parse(raw)
        self.assertFalse(self.transport.parsed)
        self.assertEqual(self.plugin.engine.state, "incoming_call")
        self.plugin.incoming_call.assert_called_once()

    def test_untrusted_origin_cannot_ring(self):
        raw = json.dumps(dict(type=self.plugin_module.MESSAGE, action="call_offer", protocol=2, token="f"*32, target_instance=self.plugin.engine.instance, origin=99)).encode()
        self.transport.parse(raw)
        self.assertEqual(self.plugin.engine.state, "idle")
        self.plugin.incoming_call.assert_not_called()

    def test_backpressure_drops_media_without_dropping_stop(self):
        for _ in range(6): self.transport.queue.put(b"key message")
        self.plugin.send(dict(action="frame", protocol=2, token="f"*32))
        self.assertEqual(self.transport.queue.qsize(), 6)
        self.plugin.send(dict(action="stop", protocol=2, token="f"*32))
        self.assertEqual(self.transport.queue.qsize(), 7)

    def test_start_with_one_candidate_skips_remote_computer_dialog(self):
        self.plugin.engine.disconnect()
        self.plugin.pair = None
        with patch.object(self.plugin_module, "HELPER", Mock(is_file=lambda: True)), patch.object(self.plugin, "on_choose_peer") as chooser:
            self.plugin.start_action("call")
        chooser.assert_not_called()
        self.assertEqual(self.plugin.engine.call.state, "outgoing_call")
        self.assertEqual(self.plugin.engine.peer, 2)

    def test_recovery_helper_is_muted_from_process_start(self):
        self.plugin.settings = {"input": "default", "output": "default", "volume": 100}
        self.plugin.engine.call.muted = True
        with patch.object(self.plugin_module, "NativeAudio") as native:
            self.plugin.start_audio("call", Mock(), Mock(), Mock())
            self.assertTrue(native.call_args.kwargs["initial_muted"])

    def test_lost_connection_announces_call_interruption(self):
        self.plugin.engine.call.state = "call"
        with patch.object(self.plugin, "notify") as notify:
            self.plugin._connection_ended()
        notify.assert_called_once_with("Remote connection lost. Start a new call when Remote Access reconnects")

    def test_network_rejoin_restores_call_to_same_instance_with_new_remote_id(self):
        self.plugin.engine.peer_call_recovery = True
        self.plugin.engine.call.state = "call"
        self.plugin.engine.call.token = "c"*32
        self.plugin.engine.call.muted = True
        self.plugin._connection_ended()
        self.assertIsNotNone(self.plugin.engine.suspended_call)
        self.plugin._connection_started(self.transport)
        self.plugin._membership(self.transport, dict(type="channel_joined", clients=[dict(id=8, connection_type="slave")]))
        self.plugin._control(self.transport, dict(action="hello", protocol=2, instance="a"*32, simultaneous=True, system_audio=True, call_recovery=True), 8)
        self.assertEqual(self.plugin.engine.peer, 8)
        self.assertEqual(self.plugin.engine.call.state, "resuming_call")
        self.assertTrue(self.plugin.engine.muted)
        self.assertEqual(self.plugin.engine.token, "c"*32)
        self.assertIsNone(self.plugin.engine.suspended_call)

    def test_explicit_remote_disconnect_cancels_recovery(self):
        self.plugin.engine.peer_call_recovery = True
        self.plugin.engine.call.state = "call"
        self.plugin.engine.call.token = "c"*32
        self.plugin.closing_handler()
        self.assertIsNone(self.plugin.engine.suspended_call)
        self.assertEqual(self.plugin.engine.state, "idle")

    def test_deferred_closing_followed_by_disconnected_cannot_resume(self):
        self.plugin.engine.peer_call_recovery = True
        self.plugin.engine.call.state = "call"
        self.plugin.engine.call.token = "c"*32
        pending = []
        with patch.object(self.plugin_module.wx, "CallAfter", side_effect=lambda *args: pending.append(args)):
            worker = threading.Thread(target=lambda: (self.plugin.closing_handler(), self.plugin.disconnect_handler()))
            worker.start()
            worker.join()
        for callback, *args in pending:
            callback(*args)
        self.assertIsNone(self.plugin.engine.suspended_call)
        self.assertEqual(self.plugin.engine.state, "idle")

    def test_lock_cancels_pending_network_recovery(self):
        self.plugin.engine.peer_call_recovery = True
        self.plugin.engine.call.state = "call"
        self.plugin.engine.call.token = "c"*32
        self.plugin._connection_ended()
        self.plugin._lock_changed(True)
        self.assertIsNone(self.plugin.engine.suspended_call)

    def test_lock_sends_stop_and_closes_helper(self):
        self.plugin.engine.state = "call"
        self.plugin.engine.token = "f"*32
        helper = Mock()
        self.plugin.native = helper
        self.plugin._lock_changed(True)
        helper.stop.assert_called_once()
        self.assertIsNone(self.plugin.native)
        self.assertEqual(self.plugin.engine.state, "idle")
        payload = json.loads(self.transport.queue.get())
        self.assertEqual(payload["action"], "stop")

    def test_detach_restores_parser_and_unregisters_events(self):
        self.plugin._detach()
        self.assertEqual(self.transport.parse, Transport.parse.__get__(self.transport))
        self.assertFalse(self.transport.transportClosing.handlers)
        self.assertFalse(self.transport.transportDisconnected.handlers)

    def test_network_disconnect_defers_dialog_cleanup_to_ui_thread(self):
        pending = []
        with patch.object(self.plugin_module.wx, "CallAfter", side_effect=lambda *args: pending.append(args)):
            worker = threading.Thread(target=self.plugin._connection_ended)
            worker.start()
            worker.join(timeout=2)
        self.assertEqual(self.plugin.engine.peer, 2)
        self.assertEqual(len(pending), 1)
        callback, *args = pending[0]
        callback(*args)
        self.assertIsNone(self.plugin.engine.peer)

    def test_old_disconnect_callback_does_not_end_new_connection(self):
        old = self.transport
        self.plugin._attach(Transport(), self.session)
        self.plugin.engine.connect("controlling", 2)
        self.plugin._finish_connection_ended(old)
        self.assertEqual(self.plugin.engine.peer, 2)

    def test_tick_discovers_builtin_remote_session_in_each_role(self):
        for role in ("controlling", "controlled"):
            self.plugin._detach()
            session = SimpleNamespace(transport=self.transport,
                leaders={} if role == "controlling" else {2: {}},
                followers={2: {}} if role == "controlling" else set())
            client = SimpleNamespace(leaderSession=session if role == "controlling" else None,
                followerSession=session if role == "controlled" else None,
                registerLocalScript=Mock(), unregisterLocalScript=Mock())
            self.plugin.remote_client = None
            with patch.dict(sys.modules, {"_remoteClient": SimpleNamespace(_remoteClient=client)}):
                self.plugin._tick()
            self.plugin._control(self.transport, dict(action="hello", protocol=2, instance="a"*32, simultaneous=True), 2)
            self.plugin.select_peer(2, announce=False)
            self.assertEqual(self.plugin.engine.role, role)
            self.assertEqual(self.plugin.engine.peer, 2)
            client.registerLocalScript.assert_called()

    def test_third_participant_does_not_disable_selected_stream(self):
        self.assertTrue(self.plugin.allowed())
        self.session.followers[3] = {}
        self.assertTrue(self.plugin.allowed())
        self.plugin.send(dict(action="frame", protocol=2, token="f"*32))
        self.assertFalse(self.transport.queue.empty())

    def remote_client(self):
        client = SimpleNamespace(leaderSession=self.session, followerSession=None,
            registerLocalScript=Mock(), unregisterLocalScript=Mock())
        self.plugin.remote_client = client
        self.plugin.last_hello = 0
        return patch.dict(sys.modules, {"_remoteClient": SimpleNamespace(_remoteClient=client)})

    def test_reconnect_on_same_transport_discards_stale_participants(self):
        self.plugin.engine.state = "call"
        self.plugin.native = Mock()
        old_helper = self.plugin.native
        self.plugin.audio_native = Mock()
        old_audio = self.plugin.audio_native
        self.transport.connected = False
        self.plugin._connection_ended(transport=self.transport)
        self.assertEqual(self.plugin.engine.state, "idle")
        old_helper.stop.assert_called_once()
        old_audio.stop.assert_called_once()
        # NVDA's collections still contain the previous ID after reconnecting.
        self.session.followers[3] = {}
        self.transport.connected = True
        with self.remote_client():
            raw = json.dumps(dict(type="channel_joined", clients=[dict(id=3, connection_type="slave")])).encode()
            self.transport.parse(raw)
        self.assertEqual(self.transport.parsed[-1], raw)
        self.assertIsNone(self.plugin.engine.peer)
        self.plugin._control(self.transport, dict(protocol=2, action="hello", instance="a"*32, simultaneous=True), 3)
        self.plugin.select_peer(3)
        self.assertTrue(self.plugin.engine.available)

    def test_network_disconnect_stops_devices_before_ui_callback(self):
        self.plugin.native = Mock()
        self.plugin.audio_native = Mock()
        helpers = self.plugin.native, self.plugin.audio_native
        pending = []
        with patch.object(self.plugin_module.wx, "CallAfter", side_effect=lambda *args: pending.append(args)):
            worker = threading.Thread(target=self.plugin._connection_ended)
            worker.start()
            worker.join(timeout=2)
        for helper in helpers:
            helper.stop.assert_called_once()
        self.assertFalse(self.plugin.allowed())
        self.assertIsNone(self.plugin.native)
        self.assertIsNone(self.plugin.audio_native)
        callback, *args = pending[0]
        callback(*args)
        self.assertIsNone(self.plugin.engine.peer)

    def test_delayed_disconnect_cannot_cancel_rejoined_same_transport(self):
        self.plugin._connection_ended()
        epoch = self.plugin.connection_epoch
        self.plugin._connection_started(self.transport)
        with self.remote_client():
            self.plugin._membership(self.transport, dict(type="channel_joined", clients=[dict(id=2, connection_type="slave")]))
        self.plugin._control(self.transport, dict(action="hello", protocol=2, instance="a"*32, simultaneous=True), 2)
        self.plugin._finish_connection_ended(self.transport, epoch)
        self.assertEqual(self.plugin.engine.peer, 2)

    def test_old_event_handler_cannot_disconnect_new_transport(self):
        callback = next(iter(self.transport.transportDisconnected.handlers))
        new_transport = Transport()
        self.plugin._attach(new_transport, self.session)
        self.plugin.engine.connect("controlling", 2)
        callback()
        self.assertEqual(self.plugin.engine.peer, 2)
        self.assertTrue(self.plugin.connection_live.is_set())

    def test_membership_changes_stop_audio_and_reenable_call_controls(self):
        self.plugin.menu = Mock()
        self.plugin.items = {name: (Mock(), None) for name in ("call", "audio", "answer", "decline", "stop", "mute", "devices", "settings", "status", "peer")}
        self.plugin.engine.state = "call"
        with self.remote_client(), patch.object(self.plugin_module, "HELPER", SimpleNamespace(is_file=lambda: True)):
            self.plugin._membership(self.transport, dict(type="channel_joined", clients=[dict(id=2, connection_type="slave"), dict(id=3, connection_type="master")]))
            self.assertIsNone(self.plugin.engine.peer)
            self.plugin.items["call"][0].Enable.assert_called_with(True)
            self.plugin._membership(self.transport, dict(type="client_left", client=dict(id=3)))
            self.plugin._control(self.transport, dict(protocol=2, action="hello", instance="a"*32, simultaneous=True), 2)
            self.plugin.items["call"][0].Enable.assert_called_with(True)

    def test_concurrent_helpers_start_play_and_stop_independently(self):
        self.plugin.settings = dict(input="default", output="default", volume=100)
        call_helper, audio_helper = Mock(), Mock()
        with patch.object(self.plugin_module, "NativeAudio", side_effect=[call_helper, audio_helper]):
            self.plugin.start_audio("call", Mock(), Mock(), Mock(), stream="call")
            self.plugin.start_audio("receive", Mock(), Mock(), Mock(), stream="audio")
        call_helper.stop.assert_not_called()
        self.plugin.play(b"voice", stream="call")
        self.plugin.play(b"stereo", stream="audio")
        call_helper.play.assert_called_once_with(b"voice")
        audio_helper.play.assert_called_once_with(b"stereo")
        self.plugin.mute(True)
        call_helper.mute.assert_called_once_with(True)
        audio_helper.mute.assert_not_called()
        self.plugin.stop_audio(stream="audio")
        self.assertIs(self.plugin.native, call_helper)
        call_helper.stop.assert_not_called()
        audio_helper.stop.assert_called_once()

    def hello(self, peer, instance=None):
        self.plugin._control(self.transport, dict(action="hello", protocol=2,
            instance=instance or str(peer)*32, simultaneous=True, system_audio=True), peer)

    def offer(self, peer, **changes):
        message = dict(action="call_offer", protocol=2, token="f"*32,
            target_instance=self.plugin.engine.instance, stream="call")
        message.update(changes)
        self.plugin._control(self.transport, message, peer)

    def menu(self):
        self.plugin.menu = Mock()
        self.plugin.items = {name: (Mock(), None) for name in
            ("call", "audio", "answer", "decline", "stop", "mute", "devices", "settings", "status", "peer")}

    def test_plain_extra_controller_and_controlled_computer_do_not_disable_buttons(self):
        self.menu()
        self.plugin.engine.state = "call"
        helper = self.plugin.native = Mock()
        with patch.object(self.plugin_module, "HELPER", SimpleNamespace(is_file=lambda: True)):
            for role in ("master", "slave"):
                self.plugin._membership(self.transport, dict(type="client_joined", client=dict(id=3, connection_type=role)))
                self.assertTrue(self.plugin.engine.available)
                self.assertEqual(self.plugin._candidates(), [2])
                self.assertEqual(self.plugin.engine.peer, 2)
                self.plugin.items["mute"][0].Enable.assert_called_with(True)
                self.plugin.items["status"][0].Enable.assert_called_with(True)
                self.plugin._membership(self.transport, dict(type="client_left", client=dict(id=3)))
        helper.stop.assert_not_called()

    def test_plain_peer_keyboard_and_speech_still_reach_nvda(self):
        self.plugin._membership(self.transport, dict(type="client_joined", client=dict(id=3, connection_type="master")))
        for kind in ("key", "speak", "set_clipboard_text", "tone"):
            message = json.dumps(dict(type=kind, origin=3, data="ordinary")).encode()
            self.transport.parse(message)
            self.assertEqual(self.transport.parsed[-1], message)
        self.assertTrue(self.plugin.engine.available)

    def test_incompatible_addon_never_becomes_an_audio_candidate(self):
        self.session.followers[3] = {}
        self.plugin._control(self.transport, dict(action="hello", protocol=1, instance="3"*32), 3)
        self.assertEqual(self.plugin._candidates(), [2])
        self.assertTrue(self.plugin.engine.available)

    def test_multiple_candidates_wait_for_explicit_selection(self):
        self.plugin.engine.disconnect()
        self.plugin.pair = None
        self.plugin.preferred_instance = None
        self.plugin.peers = {}
        self.session.followers[3] = {}
        self.plugin.discovery_until = float("inf")
        self.hello(2)
        self.hello(3)
        self.plugin.discovery_until = 0
        self.plugin._reconcile_peer()
        self.assertEqual(self.plugin._candidates(), [2, 3])
        self.assertIsNone(self.plugin.engine.peer)
        self.assertTrue(self.plugin.select_peer(3))
        self.assertEqual(self.plugin.engine.peer, 3)

    def test_switching_peer_stops_both_streams_and_addresses_old_stop(self):
        self.plugin.engine.call.state = "call"
        self.plugin.engine.call.token = "c"*32
        self.plugin.engine.audio.state = "audio"
        self.plugin.engine.audio.token = "d"*32
        old_helpers = self.plugin.native, self.plugin.audio_native = Mock(), Mock()
        self.session.followers[3] = {}
        self.hello(3)
        self.assertTrue(self.plugin.select_peer(3))
        for helper in old_helpers:
            helper.stop.assert_called_once()
        sent = []
        while not self.transport.queue.empty():
            sent.append(json.loads(self.transport.queue.get()))
        stops = [message for message in sent if message["action"] == "stop"]
        self.assertEqual(len(stops), 2)
        self.assertTrue(all(message["target_instance"] == "a"*32 for message in stops))
        self.assertEqual(self.plugin.engine.state, "idle")
        self.assertEqual(self.plugin.engine.peer, 3)

    def test_selected_peer_departure_resets_helpers_without_switching_to_observer(self):
        self.plugin.engine.state = "call"
        self.plugin.native = Mock()
        helper = self.plugin.native
        self.session.followers[3] = {}
        self.hello(3)
        self.plugin._membership(self.transport, dict(type="client_left", client=dict(id=2)))
        helper.stop.assert_called_once()
        self.assertIsNone(self.plugin.engine.peer)
        self.assertEqual(self.plugin._candidates(), [3])

    def test_reconnecting_selected_instance_with_new_remote_id_restores_controls(self):
        self.plugin._connection_ended()
        self.plugin._connection_started(self.transport)
        self.plugin._membership(self.transport, dict(type="channel_joined", clients=[
            dict(id=3, connection_type="slave"), dict(id=4, connection_type="master")]))
        self.hello(3, instance="a"*32)
        self.assertEqual(self.plugin.engine.peer, 3)
        self.assertTrue(self.plugin.engine.available)
        self.assertEqual(self.plugin.engine.state, "idle")

    def test_stale_membership_and_control_callbacks_do_not_change_new_connection(self):
        old_epoch = self.plugin.connection_epoch
        self.plugin._connection_started(self.transport)
        self.plugin._membership(self.transport, dict(type="channel_joined", clients=[dict(id=2, connection_type="slave")]))
        self.hello(2, instance="a"*32)
        self.plugin._membership(self.transport, dict(type="client_left", client=dict(id=2)), old_epoch)
        self.plugin._control(self.transport, dict(action="call_offer", protocol=2, token="f"*32,
            target_instance=self.plugin.engine.instance), 2, old_epoch)
        self.assertTrue(self.plugin.engine.available)
        self.assertEqual(self.plugin.engine.state, "idle")

    def test_addressed_invitation_from_other_peer_selects_sender_when_idle(self):
        self.session.followers[3] = {}
        self.hello(3)
        self.offer(3)
        self.assertEqual(self.plugin.engine.peer, 3)
        self.assertEqual(self.plugin.engine.state, "incoming_call")
        self.plugin.incoming_call.assert_called_once()

    def test_wrong_recipient_or_undiscovered_sender_cannot_ring_or_switch_peer(self):
        self.session.followers[3] = {}
        self.offer(3)
        self.hello(3)
        self.offer(3, target_instance="x"*32)
        self.assertEqual(self.plugin.engine.peer, 2)
        self.assertEqual(self.plugin.engine.state, "idle")
        self.plugin.incoming_call.assert_not_called()

    def test_other_peer_invitation_is_declined_while_current_pair_is_active(self):
        self.plugin.engine.state = "call"
        self.session.followers[3] = {}
        self.hello(3)
        self.offer(3)
        self.assertEqual(self.plugin.engine.peer, 2)
        self.assertEqual(self.plugin.engine.state, "call")
        sent = []
        while not self.transport.queue.empty():
            sent.append(json.loads(self.transport.queue.get()))
        declined = [message for message in sent if message["action"] == "decline"]
        self.assertEqual(len(declined), 1)
        self.assertEqual(declined[0]["target_instance"], "3"*32)

    def test_tick_attaches_before_transport_connects_and_observes_rejoin(self):
        self.plugin._detach()
        self.transport.connected = False
        with self.remote_client():
            self.plugin._tick()
        self.assertIs(self.plugin.transport, self.transport)
        self.assertFalse(self.plugin.connection_live.is_set())
        self.transport.connected = True
        for callback in self.transport.transportConnected.handlers:
            callback()
        self.plugin._membership(self.transport, dict(type="channel_joined", clients=[dict(id=2, connection_type="slave")]))
        self.hello(2, instance="a"*32)
        self.plugin.select_peer(2, announce=False)
        self.assertTrue(self.plugin.engine.available)

    def test_start_without_peer_explains_connection_instead_of_silent_disabled_action(self):
        self.plugin._detach()
        self.menu()
        with patch.object(self.plugin_module, "HELPER", SimpleNamespace(is_file=lambda: True)):
            self.plugin.changed()
            self.plugin.items["call"][0].Enable.assert_called_with(True)
            for name in ("settings", "status", "peer", "devices"):
                self.plugin.items[name][0].Enable.assert_called_with(True)
            with patch.object(self.plugin, "notify") as notification:
                self.plugin.start_action("call")
                notification.assert_called_once_with("Connect through Remote Access first")

    def test_controlled_side_keeps_selected_controller_when_plain_controller_joins(self):
        self.session.leaders = {2: {}}
        self.session.followers = {}
        self.plugin._attach(self.transport, self.session, "controlled")
        self.plugin.discovery_until = 0
        self.hello(2, instance="a"*32)
        self.plugin.engine.state = "call"
        self.plugin._membership(self.transport, dict(type="client_joined", client=dict(id=3, connection_type="master")))
        self.assertTrue(self.plugin.engine.available)
        self.assertEqual(self.plugin.engine.peer, 2)
        self.assertEqual(self.plugin.engine.state, "call")
        self.assertEqual(self.plugin._candidates(), [2])

    def test_malformed_invitation_cannot_change_selected_computer(self):
        self.session.followers[3] = {}
        self.hello(3)
        self.offer(3, token="invalid")
        self.assertEqual(self.plugin.engine.peer, 2)
        self.assertEqual(self.plugin.engine.state, "idle")

    def test_stop_remains_usable_if_helper_outlives_session_state(self):
        self.menu()
        helper = self.plugin.native = Mock()
        self.plugin.changed()
        self.plugin.items["stop"][0].Enable.assert_called_with(True)
        self.plugin.engine.stop()
        helper.stop.assert_called_once()
        self.plugin.items["stop"][0].Enable.assert_called_with(False)
