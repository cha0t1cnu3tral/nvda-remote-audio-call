"""Use installed NVDA's real wxPython to check menus without changing NVDA.

Run with a separate CPython matching NVDA's ABI (3.13 for NVDA 2026.2).
NVDA services are stubbed. No windows are shown, no stream starts, and the
running NVDA profile/process is untouched. This is not an interactive test.
"""
import argparse
import importlib.abc
import importlib.util
from pathlib import Path
import os
import queue
import sys
import tempfile
from types import ModuleType, SimpleNamespace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--nvda-dir", type=Path, default=Path("C:/Program Files/NVDA"))
    args = parser.parse_args()
    dll_directory = os.add_dll_directory(str(args.nvda_dir))
    class WxExtensionFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if not fullname.startswith("wx."):
                return None
            binary = args.nvda_dir / (fullname + ".pyd")
            if binary.is_file():
                return importlib.util.spec_from_file_location(fullname, binary)
    finder = WxExtensionFinder()
    sys.meta_path.insert(0, finder)
    sys.path.append(str(args.nvda_dir / "library.zip"))
    import wx
    print("Loaded NVDA wxPython:", wx.version())

    class Action:
        def __init__(self): self.handlers = set()
        def register(self, function): self.handlers.add(function)
        def unregister(self, function): self.handlers.discard(function)
    class BasePlugin:
        def terminate(self): pass
    class Transport:
        def __init__(self):
            self.connected = True
            self.queue = queue.Queue()
            self.transportDisconnected = Action()
            self.transportClosing = Action()
        def parse(self, line): pass

    app = wx.App(False)
    frame = wx.Frame(None, title="Hidden add-on smoke test")
    frame.sysTrayIcon = frame
    frame.toolsMenu = wx.Menu()
    frame.prePopup = frame.postPopup = lambda: None
    with tempfile.TemporaryDirectory() as temporary:
        for name in ("globalPluginHandler", "globalVars", "gui", "logHandler", "scriptHandler", "tones", "ui", "utils", "utils.security", "winAPI", "winAPI.sessionTracking", "_remoteClient"):
            sys.modules[name] = ModuleType(name)
        sys.modules["globalPluginHandler"].GlobalPlugin = BasePlugin
        sys.modules["globalVars"].appArgs = SimpleNamespace(configPath=temporary)
        sys.modules["gui"].mainFrame = frame
        sys.modules["logHandler"].log = SimpleNamespace(exception=lambda text: (_ for _ in ()).throw(AssertionError(text)))
        sys.modules["scriptHandler"].script = lambda **kwargs: lambda function: function
        sys.modules["tones"].beep = lambda *args: None
        sys.modules["ui"].message = lambda text: None
        sys.modules["utils.security"].isRunningOnSecureDesktop = lambda: False
        lock_action = Action()
        sys.modules["utils.security"].post_sessionLockStateChanged = lock_action
        sys.modules["winAPI.sessionTracking"].isLockScreenModeActive = lambda: False
        sys.modules["_remoteClient"]._remoteClient = None
        root = Path(__file__).resolve().parents[1] / "addon/globalPlugins/remoteAudioCall"
        spec = importlib.util.spec_from_file_location("nvda_ui_smoke_plugin", root / "__init__.py", submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        plugin = module.GlobalPlugin()
        assert frame.toolsMenu.GetMenuItemCount() == 1
        assert plugin.menu.GetMenuItemCount() == 9
        assert not plugin.items["call"][0].IsEnabled()
        transport = Transport()
        session = SimpleNamespace(leaders={}, followers={2: {}}, transport=transport)
        plugin._attach(transport, session)
        plugin.engine.connect("controlling", 2)
        plugin.engine.capable = True
        # Menu availability is independent of hardware; pretend the helper exists.
        module.HELPER = SimpleNamespace(is_file=lambda: True)
        plugin.changed()
        assert plugin.items["call"][0].IsEnabled()
        assert not plugin.items["audio"][0].IsEnabled()
        plugin.engine._new("incoming_call")
        assert plugin.items["answer"][0].IsEnabled()
        assert plugin.items["decline"][0].IsEnabled()
        # Build the actual incoming-call dialog, but keep it hidden.
        original_dialog = wx.Dialog
        class HiddenDialog(original_dialog):
            def Show(self, *args, **kwargs): return True
            def Raise(self): pass
            def ShowModal(self):
                for child in self.GetChildren():
                    if isinstance(child, wx.Choice):
                        child.SetSelection(1)
                return wx.ID_OK
        wx.Dialog = HiddenDialog
        try:
            plugin.incoming_call()
            assert plugin.call_dialog is not None
            plugin.start_audio = lambda kind, ready, packet, error, stream="call": ready()
            plugin.engine.answer()
            assert plugin.call_dialog is None
            assert plugin.engine.state == "waiting_call"
            # Both streams can be active without disabling microphone controls.
            plugin.engine.call.state = "call"
            plugin.engine.audio.state = "audio"
            plugin.engine.simultaneous = True
            plugin.changed()
            assert plugin.items["mute"][0].IsEnabled()
            assert plugin.items["stop"][0].IsEnabled()
            # A disconnect resets helpers/state, and a fresh same-transport
            # handshake restores the real wx menu even with stale Remote IDs.
            plugin._connection_ended()
            assert not plugin.items["stop"][0].IsEnabled()
            client = SimpleNamespace(leaderSession=session, followerSession=None,
                registerLocalScript=lambda script: None, unregisterLocalScript=lambda script: None)
            sys.modules["_remoteClient"]._remoteClient = client
            session.followers[3] = {}
            plugin._membership(transport, dict(type="channel_joined", clients=[dict(id=3, connection_type="slave")]))
            plugin.engine.receive(dict(protocol=1, action="hello", instance="f"*32, simultaneous=True), 3)
            assert plugin.items["call"][0].IsEnabled()
            plugin._show_settings({"input": [("default", "Default microphone"), ("selected-mic", "Selected microphone")],
                "output": [("default", "Default speakers"), ("selected-output", "Selected speakers")]})
            assert plugin.settings["input"] == "selected-mic"
            assert plugin.settings["output"] == "selected-output"
            assert plugin.settings_path.is_file()
        finally:
            wx.Dialog = original_dialog
        plugin.terminate()
        assert frame.toolsMenu.GetMenuItemCount() == 0
        assert not lock_action.handlers
        assert not transport.transportClosing.handlers
        assert not transport.transportDisconnected.handlers
        print("Real wx Tools menu, incoming-call dialog, Answer, microphone/speaker selection and teardown passed")
    frame.Destroy()
    app.Destroy()
    sys.meta_path.remove(finder)
    dll_directory.close()


if __name__ == "__main__":
    main()
