"""Contract tests against NVDA/wx stubs, without changing a user's NVDA profile."""
import importlib.util
import json
from pathlib import Path
import queue
import sys
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
        self.plugin.engine = module.Engine(self.plugin)
        self.plugin.call_dialog = None
        self.plugin.incoming_call = Mock()
        self.plugin.supports_system_audio = lambda: True
        self.transport = Transport()
        self.session = SimpleNamespace(leaders={}, followers={2: {}}, transport=self.transport)
        self.plugin._attach(self.transport, self.session)
        self.plugin.engine.connect("controlling", 2)
        self.plugin.engine.capable = True
        while not self.transport.queue.empty(): self.transport.queue.get()

    def tearDown(self): self.plugin._detach()

    def test_remote_keyboard_speech_clipboard_messages_are_preserved(self):
        for kind in ("key", "speak", "set_clipboard_text", "tone"):
            raw = json.dumps(dict(type=kind, data="ordinary")).encode()
            self.transport.parse(raw)
            self.assertEqual(self.transport.parsed[-1], raw)

    def test_custom_call_message_does_not_reach_remote_enum_parser(self):
        raw = json.dumps(dict(type=self.plugin_module.MESSAGE, action="call_offer", protocol=1, token="f"*32, origin=2)).encode()
        self.transport.parse(raw)
        self.assertFalse(self.transport.parsed)
        self.assertEqual(self.plugin.engine.state, "incoming_call")
        self.plugin.incoming_call.assert_called_once()

    def test_untrusted_origin_cannot_ring(self):
        raw = json.dumps(dict(type=self.plugin_module.MESSAGE, action="call_offer", protocol=1, token="f"*32, origin=99)).encode()
        self.transport.parse(raw)
        self.assertEqual(self.plugin.engine.state, "idle")
        self.plugin.incoming_call.assert_not_called()

    def test_backpressure_drops_media_without_dropping_stop(self):
        for _ in range(6): self.transport.queue.put(b"key message")
        self.plugin.send(dict(action="frame", protocol=1, token="f"*32))
        self.assertEqual(self.transport.queue.qsize(), 6)
        self.plugin.send(dict(action="stop", protocol=1, token="f"*32))
        self.assertEqual(self.transport.queue.qsize(), 7)

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

    def test_third_participant_disables_stream_immediately(self):
        self.assertTrue(self.plugin.allowed())
        self.session.followers[3] = {}
        self.assertFalse(self.plugin.allowed())
        self.plugin.send(dict(action="frame", protocol=1, token="f"*32))
        self.assertTrue(self.transport.queue.empty())

