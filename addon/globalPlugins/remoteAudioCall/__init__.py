"""NVDA Tools menu and adapter to built-in Remote Access."""
import json
import os
from pathlib import Path
import sys
import threading
import time

import globalPluginHandler
import globalVars
import gui
from logHandler import log
from scriptHandler import script
import tones
import ui
import wx
from utils.security import isRunningOnSecureDesktop, post_sessionLockStateChanged
from winAPI.sessionTracking import isLockScreenModeActive

from .engine import Engine, MESSAGE
from .native import NativeAudio, enumerate_devices, HELPER

STATUS = {
    "idle": "Off", "outgoing_call": "Calling", "incoming_call": "Incoming call",
    "answering_call": "Answering", "connecting_call": "Connecting call",
    "waiting_call": "Waiting for call connection", "call": "Call",
    "outgoing_audio": "Requesting computer audio", "preparing_receiver": "Preparing audio playback",
    "waiting_audio": "Waiting for computer audio", "preparing_sender": "Preparing computer audio capture",
    "audio": "Computer audio",
}


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    scriptCategory = "Remote Audio and Call"

    def __init__(self):
        super().__init__()
        self.closed = False
        self.locked = isLockScreenModeActive()
        self.transport = self.session = self.native = None
        self.old_parse = self.hooked_parse = None
        self.pair = None
        self.last_hello = 0
        self.call_dialog = self.settings_dialog = None
        self.engine = Engine(self)
        self.settings = {"input": "default", "output": "default", "volume": 100}
        self.settings_path = Path(globalVars.appArgs.configPath) / "remoteAudioCall.json"
        self._load_settings()
        self.menu = self.menu_item = self.timer = None
        self.items = {}
        if isRunningOnSecureDesktop():
            self.closed = True
            return
        tray = gui.mainFrame.sysTrayIcon
        self.menu = wx.Menu()
        actions = [
            ("call", "Start &call", lambda event: self.engine.start_call()),
            ("answer", "&Answer call", lambda event: self.engine.answer()),
            ("decline", "&Decline call", lambda event: self.engine.decline()),
            ("audio", "Share computer &audio (excluding NVDA)", lambda event: self.engine.start_system_audio()),
            ("stop", "&Stop audio or hang up", lambda event: self.engine.stop()),
            ("mute", "&Mute microphone", lambda event: self.engine.toggle_mute()),
            ("settings", "&Settings...", self.on_settings),
            ("status", "Report s&tatus", lambda event: self.report_status()),
        ]
        for name, label, handler in actions:
            item = self.menu.Append(wx.ID_ANY, label, kind=wx.ITEM_CHECK if name == "mute" else wx.ITEM_NORMAL)
            self.items[name] = (item, handler)
            tray.Bind(wx.EVT_MENU, handler, item)
        self.menu_item = tray.toolsMenu.AppendSubMenu(self.menu, "Remote Audio and Call")
        post_sessionLockStateChanged.register(self._lock_changed)
        self.timer = wx.Timer(tray)
        tray.Bind(wx.EVT_TIMER, self._tick, self.timer)
        self.timer.Start(200)
        self.changed()

    def _load_settings(self):
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            for key in ("input", "output"):
                if isinstance(data.get(key), str) and 0 < len(data[key]) < 1024:
                    self.settings[key] = data[key]
            if type(data.get("volume")) is int and 0 <= data["volume"] <= 100:
                self.settings["volume"] = data["volume"]
        except (OSError, ValueError, AttributeError):
            pass

    def allowed(self):
        if self.closed or self.locked or isRunningOnSecureDesktop() or isLockScreenModeActive():
            return False
        if not self.transport or not self.transport.connected or not self.session:
            return False
        return len(self.session.leaders) + len(self.session.followers) == 1

    def supports_system_audio(self):
        return sys.getwindowsversion().build >= 20348 and HELPER.is_file()

    def notify(self, text):
        if not self.closed:
            ui.message(text)

    def changed(self):
        if not self.menu or self.closed:
            return
        ready = self.engine.available and HELPER.is_file()
        state = self.engine.state
        self.items["call"][0].Enable(ready and state in ("idle", "audio"))
        self.items["audio"][0].Enable(ready and self.engine.role == "controlled" and self.supports_system_audio() and state in ("idle", "call"))
        for name in ("answer", "decline"):
            self.items[name][0].Enable(ready and state == "incoming_call")
        self.items["stop"][0].Enable(state != "idle")
        self.items["mute"][0].Enable(state == "call")
        self.items["mute"][0].Check(self.engine.muted)
        if self.call_dialog and state != "incoming_call":
            dialog, self.call_dialog = self.call_dialog, None
            dialog.Destroy()

    def incoming_call(self):
        self.notify("Incoming remote call. Answer or decline in Tools, Remote Audio and Call")
        self._ring(self.engine.token)
        if self.call_dialog:
            self.call_dialog.Destroy()
        dialog = wx.Dialog(gui.mainFrame, title="Incoming Remote Call", style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP)
        self.call_dialog = dialog
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(wx.StaticText(dialog, label="The other person is calling. Answer to enable your microphone."), 0, wx.ALL, 12)
        row = wx.BoxSizer(wx.HORIZONTAL)
        answer = wx.Button(dialog, label="&Answer")
        decline = wx.Button(dialog, label="&Decline")
        row.Add(answer, 0, wx.ALL, 6)
        row.Add(decline, 0, wx.ALL, 6)
        layout.Add(row, 0, wx.ALIGN_RIGHT | wx.ALL, 6)
        dialog.SetSizerAndFit(layout)
        answer.Bind(wx.EVT_BUTTON, lambda event: self.engine.answer())
        decline.Bind(wx.EVT_BUTTON, lambda event: self.engine.decline())
        dialog.Bind(wx.EVT_CLOSE, lambda event: self.engine.decline())
        dialog.Bind(wx.EVT_CHAR_HOOK, lambda event: self.engine.decline() if event.GetKeyCode() == wx.WXK_ESCAPE else event.Skip())
        gui.mainFrame.prePopup()
        dialog.Show()
        dialog.Raise()
        answer.SetFocus()
        gui.mainFrame.postPopup()

    def _ring(self, token):
        if not self.closed and not self.locked and self.engine.state == "incoming_call" and self.engine.token == token:
            tones.beep(660, 120)
            wx.CallLater(2500, self._ring, token)

    def start_audio(self, kind, ready, packet, error):
        self.stop_audio()
        try:
            self.native = NativeAudio(kind, self.settings.copy(), ready, packet, error, wx.CallAfter)
        except (OSError, ValueError) as exc:
            wx.CallAfter(error, str(exc))

    def stop_audio(self):
        native, self.native = self.native, None
        if native:
            native.stop()

    def play(self, packet):
        native = self.native
        if native:
            native.play(packet)

    def mute(self, muted):
        if self.native:
            self.native.mute(muted)

    def send(self, payload):
        transport = self.transport
        if not transport or not transport.connected:
            return
        if payload["action"] != "stop" and not self.allowed():
            return
        if payload["action"] == "frame" and transport.queue.qsize() >= 6:
            return
        # Serialize our own message directly, avoiding Remote debug logging of audio.
        data = json.dumps(dict(type=MESSAGE, **payload), separators=(",", ":")).encode("utf-8") + b"\n"
        transport.queue.put(data)

    def _attach(self, transport, session):
        self._detach()
        self.transport, self.session = transport, session
        self.old_parse = transport.parse
        old_parse = self.old_parse
        def parse(line):
            # Ordinary Remote messages are always handled by NVDA's original parser.
            # Parse plain JSON first to avoid NVDA-specific deserialization of audio.
            try:
                data = json.loads(line)
            except (ValueError, UnicodeError):
                return old_parse(line)
            if not isinstance(data, dict) or data.get("type") != MESSAGE:
                return old_parse(line)
            if len(line) > 4096 or self.transport is not transport:
                return
            origin = data.get("origin")
            if data.get("action") == "frame":
                self.engine.receive_frame(data, origin)
            else:
                wx.CallAfter(self._control, transport, data, origin)
        self.hooked_parse = parse
        transport.parse = parse
        transport.transportDisconnected.register(self._connection_ended)
        transport.transportClosing.register(self._connection_ended)

    def _control(self, transport, data, origin):
        if self.transport is transport:
            self.engine.receive(data, origin)

    def _connection_ended(self, **kwargs):
        self.engine.disconnect()
        self.pair = None

    def _detach(self):
        self.engine.disconnect()
        transport = self.transport
        if transport:
            if transport.parse is self.hooked_parse:
                transport.parse = self.old_parse
            transport.transportDisconnected.unregister(self._connection_ended)
            transport.transportClosing.unregister(self._connection_ended)
        self.transport = self.session = self.old_parse = self.hooked_parse = self.pair = None

    def _tick(self, event=None):
        if self.closed:
            return
        try:
            import _remoteClient
            client = _remoteClient._remoteClient
            session = (client.leaderSession or client.followerSession) if client else None
            transport = session.transport if session else None
            if transport and transport.connected:
                if transport is not self.transport:
                    self._attach(transport, session)
                role = "controlling" if session is client.leaderSession else "controlled"
                others = session.followers if role == "controlling" else session.leaders
                wrong_role = session.leaders if role == "controlling" else session.followers
                peer = next(iter(others)) if len(others) == 1 and not wrong_role else None
                pair = (role, peer) if peer is not None and self.allowed() else None
                if pair != self.pair:
                    if pair:
                        self.engine.connect(*pair)
                    else:
                        self.engine.disconnect()
                    self.pair = pair
                    self.last_hello = 0
                if pair and time.monotonic() - self.last_hello >= 2:
                    self.engine.hello()
                    self.last_hello = time.monotonic()
            elif self.transport:
                self._detach()
            self.engine.tick()
            self.changed()
        except Exception:
            log.exception("Remote Audio and Call connection adapter failed")
            self._detach()

    def _lock_changed(self, isNowLocked):
        self.locked = isNowLocked
        if isNowLocked:
            self.engine.stop(announce=False)
            self.engine.disconnect()
            self.pair = None

    def report_status(self):
        if not HELPER.is_file():
            self.notify("Audio helper missing. Reinstall the packaged add-on")
        elif self.engine.peer is None:
            self.notify("Off. Connect exactly one controller and one controlled computer in Remote Access")
        elif not self.engine.capable:
            self.notify("Off. Waiting for the other computer's add-on. Both computers must install Remote Audio and Call")
        else:
            state = STATUS[self.engine.state]
            if self.engine.state == "audio":
                state = "Sharing computer audio" if self.engine.role == "controlled" else "Listening to computer audio"
            suffix = ". Microphone muted" if self.engine.muted else ""
            self.notify(state + suffix)

    def on_settings(self, event=None):
        if self.settings_dialog:
            self.settings_dialog.Raise()
            return
        self.notify("Loading audio devices")
        def load():
            try:
                devices = enumerate_devices()
                wx.CallAfter(self._show_settings, devices)
            except (OSError, RuntimeError, TimeoutError) as exc:
                wx.CallAfter(self.notify, "Cannot list audio devices: " + str(exc))
            except Exception:
                log.exception("Cannot enumerate audio devices")
                wx.CallAfter(self.notify, "Cannot list audio devices")
        threading.Thread(target=load, daemon=True, name="RemoteAudioDevices").start()

    def _show_settings(self, devices):
        if self.closed or self.locked:
            return
        if self.settings_dialog:
            self.settings_dialog.Raise()
            return
        dialog = wx.Dialog(gui.mainFrame, title="Remote Audio and Call Settings")
        self.settings_dialog = dialog
        layout = wx.BoxSizer(wx.VERTICAL)
        controls = {}
        for key, label in (("input", "&Microphone:"), ("output", "&Playback device:")):
            layout.Add(wx.StaticText(dialog, label=label), 0, wx.LEFT | wx.TOP, 12)
            choice = wx.Choice(dialog, choices=[name for _, name in devices[key]])
            ids = [device for device, _ in devices[key]]
            selected = ids.index(self.settings[key]) if self.settings[key] in ids else 0
            choice.SetSelection(selected)
            layout.Add(choice, 0, wx.EXPAND | wx.ALL, 12)
            controls[key] = choice
        layout.Add(wx.StaticText(dialog, label="Listening &volume (0 to 100):"), 0, wx.LEFT, 12)
        volume = wx.SpinCtrl(dialog, min=0, max=100, initial=self.settings["volume"])
        layout.Add(volume, 0, wx.EXPAND | wx.ALL, 12)
        layout.Add(wx.StaticText(dialog, label="Device changes apply next time audio starts. Use headphones for calls.\nComputer audio excludes NVDA across all output devices."), 0, wx.ALL, 12)
        layout.Add(dialog.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 12)
        dialog.SetSizerAndFit(layout)
        gui.mainFrame.prePopup()
        try:
            if dialog.ShowModal() == wx.ID_OK:
                updated = {key: devices[key][controls[key].GetSelection()][0] for key in controls}
                updated["volume"] = volume.GetValue()
                try:
                    temporary = self.settings_path.with_suffix(".tmp")
                    temporary.write_text(json.dumps(updated), encoding="utf-8")
                    os.replace(temporary, self.settings_path)
                    self.settings = updated
                    if self.native:
                        self.native.set_volume(updated["volume"])
                    self.notify("Audio settings saved")
                except OSError as exc:
                    self.notify("Cannot save audio settings: " + str(exc))
        finally:
            self.settings_dialog = None
            dialog.Destroy()
            gui.mainFrame.postPopup()

    @script(description="Start a voice call with the other remote computer")
    def script_startCall(self, gesture):
        self.engine.start_call()

    @script(description="Answer an incoming remote call")
    def script_answerCall(self, gesture):
        self.engine.answer()

    @script(description="Share the controlled computer's audio, excluding NVDA speech")
    def script_shareAudio(self, gesture):
        self.engine.start_system_audio()

    @script(description="Stop remote audio or hang up the call")
    def script_stop(self, gesture):
        self.engine.stop()

    @script(description="Mute or unmute the call microphone")
    def script_muteMicrophone(self, gesture):
        self.engine.toggle_mute()

    @script(description="Report remote audio and call status")
    def script_reportStatus(self, gesture):
        self.report_status()

    def terminate(self):
        if self.closed:
            super().terminate()
            return
        self.engine.stop(announce=False)
        self.closed = True
        self._detach()
        post_sessionLockStateChanged.unregister(self._lock_changed)
        tray = gui.mainFrame.sysTrayIcon
        if self.timer:
            self.timer.Stop()
            tray.Unbind(wx.EVT_TIMER, handler=self._tick, source=self.timer)
        if self.call_dialog:
            self.call_dialog.Destroy()
            self.call_dialog = None
        if self.settings_dialog:
            self.settings_dialog.EndModal(wx.ID_CANCEL)
        for item, handler in self.items.values():
            tray.Unbind(wx.EVT_MENU, handler=handler, source=item)
        if self.menu_item:
            tray.toolsMenu.DestroyItem(self.menu_item)
        super().terminate()
