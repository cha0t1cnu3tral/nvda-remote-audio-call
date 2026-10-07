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
        self.audio_native = None
        self.members = None
        self.connection_epoch = 0
        self.connection_live = threading.Event()
        self.old_parse = self.hooked_parse = None
        self.pair = None
        self.remote_client = None
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
            ("devices", "Choose &microphone and speakers...", self.on_settings),
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
        if not self.connection_live.is_set():
            return False
        leaders, followers = self._participants()
        count = len(self.members) if self.members is not None else len(leaders) + len(followers)
        return count == 1 and len(leaders) + len(followers) == 1

    def _participants(self):
        members = self.members
        if members is not None:
            return ({peer for peer, role in members.items() if role == "master"},
                    {peer for peer, role in members.items() if role == "slave"})
        return self.session.leaders, self.session.followers

    def supports_system_audio(self):
        return sys.getwindowsversion().build >= 20348 and HELPER.is_file()

    def notify(self, text):
        if not self.closed:
            ui.message(text)

    def changed(self):
        if not self.menu or self.closed:
            return
        ready = self.engine.available and HELPER.is_file()
        state = self.engine.call.state
        self.items["call"][0].Enable(ready and state == "idle")
        self.items["audio"][0].Enable(ready and self.engine.role == "controlled" and self.supports_system_audio() and self.engine.audio.state == "idle" and (state == "idle" or self.engine.simultaneous))
        for name in ("answer", "decline"):
            self.items[name][0].Enable(ready and state == "incoming_call")
        self.items["stop"][0].Enable(self.engine.state != "idle")
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
        devices = wx.Button(dialog, label="&Microphone and speakers...")
        row.Add(devices, 0, wx.ALL, 6)
        layout.Add(row, 0, wx.ALIGN_RIGHT | wx.ALL, 6)
        dialog.SetSizerAndFit(layout)
        answer.Bind(wx.EVT_BUTTON, lambda event: self.engine.answer())
        decline.Bind(wx.EVT_BUTTON, lambda event: self.engine.decline())
        devices.Bind(wx.EVT_BUTTON, self.on_settings)
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

    def start_audio(self, kind, ready, packet, error, stream="call"):
        self.stop_audio(stream=stream)
        try:
            native = NativeAudio(kind, self.settings.copy(), ready, packet, error, wx.CallAfter)
            setattr(self, "audio_native" if stream == "audio" else "native", native)
        except (OSError, ValueError) as exc:
            wx.CallAfter(error, str(exc))

    def stop_audio(self, stream="call"):
        attribute = "audio_native" if stream == "audio" else "native"
        native = getattr(self, attribute, None)
        setattr(self, attribute, None)
        if native:
            native.stop()

    def play(self, packet, stream="call"):
        native = self.audio_native if stream == "audio" else self.native
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
        self.connection_live.set()
        self.old_parse = transport.parse
        old_parse = self.old_parse
        def parse(line):
            # Ordinary Remote messages are always handled by NVDA's original parser.
            # Parse plain JSON first to avoid NVDA-specific deserialization of audio.
            try:
                data = json.loads(line)
            except (ValueError, UnicodeError):
                return old_parse(line)
            if isinstance(data, dict) and data.get("type") in ("channel_joined", "client_joined", "client_left"):
                wx.CallAfter(self._membership, transport, data)
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
        # Bind the source transport, rather than looking up a potentially newer one.
        self.disconnect_handler = lambda **kwargs: self._connection_ended(transport=transport)
        transport.transportDisconnected.register(self.disconnect_handler)
        transport.transportClosing.register(self.disconnect_handler)

    def _membership(self, transport, data):
        if self.transport is not transport:
            return
        if data["type"] == "channel_joined":
            self.connection_epoch += 1
            self.engine.disconnect()
            self.pair = None
            self.members = {client["id"]: client.get("connection_type") for client in data.get("clients", [])}
            self.connection_live.set()
        else:
            if self.members is None:
                leaders, followers = self._participants()
                members = dict.fromkeys(leaders, "master")
                members.update(dict.fromkeys(followers, "slave"))
            else:
                members = self.members.copy()
            client = data.get("client") or {}
            peer = client.get("id")
            if data["type"] == "client_left":
                members.pop(peer, None)
            elif peer is not None:
                members[peer] = client.get("connection_type")
            # Reader-thread permission checks see an immutable snapshot.
            self.members = members
        # Stop old helpers and refresh controls as soon as membership changes.
        self._tick()

    def _control(self, transport, data, origin):
        if self.transport is transport:
            self.engine.receive(data, origin)

    def _connection_ended(self, transport=None, **kwargs):
        # Remote invokes disconnect handlers on its network thread. wx menus
        # and the incoming-call dialog must only be touched on the UI thread.
        transport = transport or self.transport
        if self.transport is not transport:
            return
        epoch = self.connection_epoch
        self.connection_live.clear()
        # Device teardown is thread-safe; stop capture/playback immediately even
        # when NVDA's UI is busy and the state reset has to wait for CallAfter.
        self.stop_audio(stream="call")
        self.stop_audio(stream="audio")
        if threading.current_thread() is not threading.main_thread():
            wx.CallAfter(self._finish_connection_ended, transport, epoch)
            return
        self._finish_connection_ended(transport, epoch)

    def _finish_connection_ended(self, transport, epoch=None):
        if self.transport is not transport or (epoch is not None and epoch != self.connection_epoch):
            return
        self.engine.disconnect()
        self.pair = None
        self.members = {}
        self.changed()

    def _detach(self):
        self.engine.disconnect()
        transport = self.transport
        if transport:
            if transport.parse is self.hooked_parse:
                transport.parse = self.old_parse
            transport.transportDisconnected.unregister(self.disconnect_handler)
            transport.transportClosing.unregister(self.disconnect_handler)
        self.transport = self.session = self.old_parse = self.hooked_parse = self.pair = None
        self.members = None
        self.connection_live.clear()
        self.connection_epoch += 1

    def _tick(self, event=None):
        if self.closed:
            return
        try:
            import _remoteClient
            client = _remoteClient._remoteClient
            if client is not self.remote_client:
                self._local_scripts(self.remote_client, register=False)
                self.remote_client = client
                self._local_scripts(client, register=True)
            session = (client.leaderSession or client.followerSession) if client else None
            transport = session.transport if session else None
            if transport and transport.connected:
                if transport is not self.transport or session is not self.session:
                    self._attach(transport, session)
                role = "controlling" if session is client.leaderSession else "controlled"
                leaders, followers = self._participants()
                others = followers if role == "controlling" else leaders
                wrong_role = leaders if role == "controlling" else followers
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
            elif self.transport and transport is not self.transport:
                self._detach()
            elif self.transport and self.connection_live.is_set():
                self._connection_ended(transport=self.transport)
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

    def _local_scripts(self, client, register):
        if client is None:
            return
        method = client.registerLocalScript if register else client.unregisterLocalScript
        for name in ("startCall", "answerCall", "shareAudio", "stop", "muteMicrophone", "reportStatus"):
            method(getattr(self, "script_" + name))

    def report_status(self):
        if not HELPER.is_file():
            self.notify("Audio helper missing. Reinstall the packaged add-on")
        elif self.engine.peer is None:
            leaders, followers = self._participants() if self.session else ((), ())
            count = len(leaders) + len(followers)
            if count > 1:
                self.notify("Off. More than one other computer is connected. Calls require exactly one controller and one controlled computer")
            else:
                self.notify("Off. Connect exactly one controller and one controlled computer in Remote Access")
        elif not self.engine.capable:
            self.notify("Off. Waiting for the other computer's add-on. Both computers must install Remote Audio and Call")
        else:
            state = STATUS[self.engine.state]
            if self.engine.state == "audio":
                state = "Sharing computer audio" if self.engine.role == "controlled" else "Listening to computer audio"
            elif self.engine.call.state == "call" and self.engine.audio.state == "audio":
                state = "Call and computer audio"
            suffix = ". Microphone muted" if self.engine.muted else ""
            if self.engine.state == "call" and self.native:
                captured, decoded, rendered, peak = self.native.stats
                if not captured:
                    suffix += ". Waiting for microphone audio"
                elif not decoded:
                    suffix += ". No audio received from the other computer"
                elif not rendered:
                    suffix += ". Audio received, waiting for playback"
                elif not self.engine.muted and peak == 0:
                    suffix += ". Microphone is silent. Check the selected microphone"
                else:
                    suffix += ". Two-way audio is flowing"
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
            choice.SetName("Microphone" if key == "input" else "Playback device")
            ids = [device for device, _ in devices[key]]
            selected = ids.index(self.settings[key]) if self.settings[key] in ids else 0
            choice.SetSelection(selected)
            layout.Add(choice, 0, wx.EXPAND | wx.ALL, 12)
            controls[key] = choice
        layout.Add(wx.StaticText(dialog, label="Listening &volume (0 to 100):"), 0, wx.LEFT, 12)
        volume = wx.SpinCtrl(dialog, min=0, max=100, initial=self.settings["volume"])
        volume.SetName("Listening volume")
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
                    if self.audio_native:
                        self.audio_native.set_volume(updated["volume"])
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
        self._local_scripts(self.remote_client, register=False)
        self.remote_client = None
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
