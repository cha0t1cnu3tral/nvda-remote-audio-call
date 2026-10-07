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

from .engine import Engine, MESSAGE, PROTOCOL
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
        self._init_connection_state()
        self.old_parse = self.hooked_parse = None
        self.pair = None
        self.remote_client = None
        self.last_hello = 0
        self.call_dialog = self.settings_dialog = self.peer_dialog = None
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
            ("call", "Start &call", lambda event: self.start_action("call")),
            ("answer", "&Answer call", lambda event: self.engine.answer()),
            ("decline", "&Decline call", lambda event: self.engine.decline()),
            ("audio", "Share computer &audio (excluding NVDA)", lambda event: self.start_action("audio")),
            ("stop", "&Stop audio or hang up", lambda event: self.engine.stop()),
            ("mute", "&Mute microphone", lambda event: self.engine.toggle_mute()),
            ("devices", "Choose &microphone and speakers...", self.on_settings),
            ("peer", "Choose &remote computer...", self.on_choose_peer),
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

    def _init_connection_state(self):
        self.members = None
        self.peers = {}
        self.preferred_instance = None
        self.local_role = None
        self.connection_epoch = 0
        self.connection_live = threading.Event()
        self.discovery_until = 0
        self.peer_dialog = None

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
        if not self.pair:
            return False
        role, peer, instance = self.pair
        info = self.peers.get(peer)
        return role == self.local_role and peer in self._opposite_peers() and info is not None and info["instance"] == instance

    def _participants(self):
        members = self.members
        if members is not None:
            return ({peer for peer, role in members.items() if role == "master"},
                    {peer for peer, role in members.items() if role == "slave"})
        return self.session.leaders, self.session.followers

    def _opposite_peers(self):
        if not self.session:
            return ()
        leaders, followers = self._participants()
        return followers if self.local_role == "controlling" else leaders

    def _candidates(self):
        return sorted(peer for peer in self._opposite_peers() if peer in self.peers)

    def _reconcile_peer(self):
        if not self.transport or not self.transport.connected or not self.connection_live.is_set():
            return
        candidates = self._candidates()
        if self.pair:
            _, peer, instance = self.pair
            if peer in candidates and self.peers[peer]["instance"] == instance:
                return
            self.engine.disconnect()
            self.pair = None
            log.info("Remote Audio and Call selected computer disconnected")
        preferred = [peer for peer in candidates if self.peers[peer]["instance"] == self.preferred_instance]
        if len(preferred) == 1:
            self.select_peer(preferred[0], announce=False)
        elif self.preferred_instance is None and len(candidates) == 1 and time.monotonic() >= self.discovery_until:
            self.select_peer(candidates[0], announce=False)

    def select_peer(self, peer, announce=True):
        if peer not in self._candidates() or not self.connection_live.is_set() or not self.transport.connected:
            self.notify("That computer is no longer available. Choose a connected computer again")
            return False
        info = self.peers[peer]
        pair = (self.local_role, peer, info["instance"])
        if pair != self.pair:
            self.engine.stop(announce=False)
            self.pair = pair
            self.preferred_instance = info["instance"]
            self.engine.connect(self.local_role, peer, info["instance"])
            self.engine.receive(dict(info, action="hello_ack", protocol=PROTOCOL, target_instance=self.engine.instance), peer)
            log.info("Remote Audio and Call selected remote computer %s; local role %s", peer, self.local_role)
        self.changed()
        if announce:
            self.notify("Selected remote computer " + str(peer))
        return True

    def availability_reason(self):
        if self.locked or isRunningOnSecureDesktop() or isLockScreenModeActive():
            return "Audio is unavailable while this computer is locked or on a secure desktop"
        if not HELPER.is_file():
            return "Audio helper missing. Reinstall the packaged add-on"
        if not self.transport or not self.transport.connected:
            return "Connect through Remote Access first"
        if not self.connection_live.is_set():
            return "Waiting for Remote Access to rejoin the channel"
        if not self._candidates():
            return "Waiting for a compatible opposite-role computer. Install Remote Audio and Call 0.1.3 or newer on both computers"
        return "Choose a remote computer from Tools, Remote Audio and Call"

    def start_action(self, kind):
        if not self.engine.available:
            if self._candidates() and not self.locked and not isRunningOnSecureDesktop() and not isLockScreenModeActive() and HELPER.is_file():
                if not self.on_choose_peer():
                    return
            else:
                self.notify(self.availability_reason())
                return
        if kind == "call":
            self.engine.start_call()
        else:
            self.engine.start_system_audio()

    def on_choose_peer(self, event=None):
        if self.peer_dialog:
            self.peer_dialog.Raise()
            return False
        candidates = self._candidates()
        if not candidates:
            self.notify(self.availability_reason())
            return False
        # Capture identities so a reused Remote ID cannot select a different
        # computer while the modal dialog is open.
        identities = {peer: self.peers[peer]["instance"] for peer in candidates}
        role = "Controlled computer" if self.local_role == "controlling" else "Controller"
        dialog = wx.Dialog(gui.mainFrame, title="Choose Remote Computer")
        self.peer_dialog = dialog
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(wx.StaticText(dialog, label="Choose the computer for calls and shared audio. Changing computers stops current audio."), 0, wx.ALL, 12)
        choice = wx.Choice(dialog, choices=[role + " " + str(peer) for peer in candidates])
        choice.SetName("Remote computer")
        choice.SetSelection(candidates.index(self.engine.peer) if self.engine.peer in candidates else 0)
        layout.Add(choice, 0, wx.EXPAND | wx.ALL, 12)
        layout.Add(dialog.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 12)
        dialog.SetSizerAndFit(layout)
        gui.mainFrame.prePopup()
        try:
            if dialog.ShowModal() == wx.ID_OK:
                peer = candidates[choice.GetSelection()]
                if self.peers.get(peer, {}).get("instance") != identities[peer]:
                    self.notify("That computer disconnected. Choose a connected computer again")
                    return False
                return self.select_peer(peer)
            return False
        finally:
            self.peer_dialog = None
            dialog.Destroy()
            gui.mainFrame.postPopup()

    def supports_system_audio(self):
        return sys.getwindowsversion().build >= 20348 and HELPER.is_file()

    def notify(self, text):
        if not self.closed:
            ui.message(text)

    def changed(self):
        if not self.menu or self.closed:
            return
        unlocked = not self.locked and not isRunningOnSecureDesktop() and not isLockScreenModeActive()
        ready = self.engine.available and HELPER.is_file()
        state = self.engine.call.state
        self.items["call"][0].Enable(unlocked and HELPER.is_file() and state == "idle")
        self.items["audio"][0].Enable(unlocked and HELPER.is_file() and self.local_role == "controlled" and self.supports_system_audio() and self.engine.audio.state == "idle")
        for name in ("answer", "decline"):
            self.items[name][0].Enable(ready and state == "incoming_call")
        self.items["stop"][0].Enable(self.engine.state != "idle" or self.native is not None or self.audio_native is not None)
        self.items["mute"][0].Enable(state == "call")
        self.items["mute"][0].Check(self.engine.muted)
        for name in ("devices", "settings", "status", "peer"):
            self.items[name][0].Enable(True)
        if self.call_dialog and state != "incoming_call":
            dialog, self.call_dialog = self.call_dialog, None
            dialog.Destroy()

    def incoming_call(self):
        self.notify("Incoming remote call from computer " + str(self.engine.peer) + ". Answer or decline in Tools, Remote Audio and Call")
        self._ring(self.engine.token)
        if self.call_dialog:
            self.call_dialog.Destroy()
        dialog = wx.Dialog(gui.mainFrame, title="Incoming Remote Call", style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP)
        self.call_dialog = dialog
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(wx.StaticText(dialog, label="Remote computer " + str(self.engine.peer) + " is calling. Answer to enable your microphone."), 0, wx.ALL, 12)
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
        if payload["action"] not in ("hello", "hello_ack", "stop", "decline") and not self.allowed():
            return
        if payload["action"] in ("hello", "hello_ack") and not self.connection_live.is_set():
            return
        if payload["action"] == "frame" and transport.queue.qsize() >= 6:
            return
        # Serialize our own message directly, avoiding Remote debug logging of audio.
        data = json.dumps(dict(type=MESSAGE, **payload), separators=(",", ":")).encode("utf-8") + b"\n"
        transport.queue.put(data)

    def _discover(self):
        if self.connection_live.is_set() and self.transport and self.transport.connected:
            self.send(dict(action="hello", protocol=PROTOCOL, instance=self.engine.instance,
                           simultaneous=True, system_audio=self.supports_system_audio()))

    def _attach(self, transport, session, role=None):
        self._detach()
        self.transport, self.session = transport, session
        self.local_role = role
        if transport.connected:
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
                wx.CallAfter(self._membership, transport, data, self.connection_epoch)
            if not isinstance(data, dict) or data.get("type") != MESSAGE:
                return old_parse(line)
            if len(line) > 4096 or self.transport is not transport:
                return
            origin = data.get("origin")
            if data.get("action") == "frame":
                if self.connection_live.is_set():
                    self.engine.receive_frame(data, origin)
            else:
                wx.CallAfter(self._control, transport, data, origin, self.connection_epoch)
        self.hooked_parse = parse
        transport.parse = parse
        # Bind the source transport, rather than looking up a potentially newer one.
        self.disconnect_handler = lambda **kwargs: self._connection_ended(transport=transport)
        self.connect_handler = lambda **kwargs: self._connection_started(transport)
        transport.transportDisconnected.register(self.disconnect_handler)
        transport.transportClosing.register(self.disconnect_handler)
        transport.transportConnected.register(self.connect_handler)
        self.last_hello = 0
        self.discovery_until = time.monotonic() + 2

    def _membership(self, transport, data, epoch=None):
        if self.transport is not transport or (epoch is not None and epoch != self.connection_epoch):
            return
        if data["type"] == "channel_joined":
            self.engine.disconnect()
            self.pair = None
            self.peers = {}
            self.members = {client["id"]: client.get("connection_type") for client in data.get("clients", [])}
            self.connection_live.set()
            self.last_hello = 0
            self.discovery_until = time.monotonic() + 2
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
                self.peers = {identity: info for identity, info in self.peers.items() if identity != peer}
            elif peer is not None:
                members[peer] = client.get("connection_type")
            # Reader-thread permission checks see an immutable snapshot.
            self.members = members
        # Stop old helpers and refresh controls as soon as membership changes.
        self._reconcile_peer()
        self._discover()
        self.changed()

    def _control(self, transport, data, origin, epoch=None):
        if self.transport is not transport or (epoch is not None and epoch != self.connection_epoch) or not self.connection_live.is_set():
            return
        if data.get("protocol") != PROTOCOL or origin not in self._opposite_peers():
            return
        action = data.get("action")
        if action in ("hello", "hello_ack"):
            if action == "hello_ack" and data.get("target_instance") != self.engine.instance:
                return
            instance = data.get("instance")
            if not isinstance(instance, str) or len(instance) != 32:
                return
            info = dict(instance=instance, simultaneous=data.get("simultaneous") is True,
                        system_audio=data.get("system_audio") is True)
            peers = self.peers.copy()
            peers[origin] = info
            self.peers = peers
            self._reconcile_peer()
            if action == "hello":
                self.send(dict(action="hello_ack", protocol=PROTOCOL, target=origin,
                    target_instance=instance, instance=self.engine.instance,
                    simultaneous=True, system_audio=self.supports_system_audio()))
            if origin == self.engine.peer:
                self.engine.receive(dict(data, target_instance=self.engine.instance), origin)
            self.changed()
            return
        if data.get("target_instance") != self.engine.instance or origin not in self.peers:
            return
        if action in ("call_offer", "audio_offer") and origin != self.engine.peer:
            token = data.get("token")
            if not isinstance(token, str) or len(token) != 32:
                return
            if action == "audio_offer" and (self.local_role != "controlling" or not self.peers[origin]["system_audio"]):
                return
            if self.engine.state == "idle" and not self.locked and not isRunningOnSecureDesktop() and not isLockScreenModeActive():
                self.select_peer(origin, announce=False)
            else:
                token = data.get("token")
                if isinstance(token, str) and len(token) == 32:
                    self.send(dict(action="decline", protocol=PROTOCOL, target=origin,
                        target_instance=self.peers[origin]["instance"], token=token, stream=data.get("stream")))
                return
        self.engine.receive(data, origin)

    def _connection_started(self, transport):
        if self.transport is not transport:
            return
        self.connection_epoch += 1
        epoch = self.connection_epoch
        self.connection_live.clear()
        self.stop_audio(stream="call")
        self.stop_audio(stream="audio")
        wx.CallAfter(self._finish_connection_started, transport, epoch)

    def _finish_connection_started(self, transport, epoch):
        if self.transport is not transport or epoch != self.connection_epoch:
            return
        self.engine.disconnect()
        self.pair = None
        self.peers = {}
        self.members = {}
        self.last_hello = 0
        log.info("Remote Audio and Call transport connected; waiting for channel membership")
        self.changed()

    def _connection_ended(self, transport=None, **kwargs):
        # Remote invokes disconnect handlers on its network thread. wx menus
        # and the incoming-call dialog must only be touched on the UI thread.
        transport = transport or self.transport
        if self.transport is not transport:
            return
        self.connection_epoch += 1
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
        self.peers = {}
        log.info("Remote Audio and Call connection ended; audio sessions reset")
        self.changed()

    def _detach(self):
        self.engine.disconnect()
        transport = self.transport
        if transport:
            if transport.parse is self.hooked_parse:
                transport.parse = self.old_parse
            transport.transportDisconnected.unregister(self.disconnect_handler)
            transport.transportClosing.unregister(self.disconnect_handler)
            transport.transportConnected.unregister(self.connect_handler)
        self.transport = self.session = self.old_parse = self.hooked_parse = self.pair = None
        self.members = None
        self.peers = {}
        self.preferred_instance = None
        self.local_role = None
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
            if transport:
                role = "controlling" if session is client.leaderSession else "controlled"
                if transport is not self.transport or session is not self.session:
                    self._attach(transport, session, role)
                self._reconcile_peer()
                if transport.connected and self.connection_live.is_set() and time.monotonic() - self.last_hello >= 2:
                    self._discover()
                    self.last_hello = time.monotonic()
                elif not transport.connected and self.connection_live.is_set():
                    self._connection_ended(transport=self.transport)
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

    def _local_scripts(self, client, register):
        if client is None:
            return
        method = client.registerLocalScript if register else client.unregisterLocalScript
        for name in ("startCall", "answerCall", "shareAudio", "stop", "muteMicrophone", "reportStatus", "chooseRemoteComputer"):
            method(getattr(self, "script_" + name))

    def report_status(self):
        if not self.engine.available or not HELPER.is_file():
            self.notify("Off. " + self.availability_reason())
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
            self.notify(state + ". Remote computer " + str(self.engine.peer) + suffix)

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
        self.start_action("call")

    @script(description="Answer an incoming remote call")
    def script_answerCall(self, gesture):
        self.engine.answer()

    @script(description="Share the controlled computer's audio, excluding NVDA speech")
    def script_shareAudio(self, gesture):
        self.start_action("audio")

    @script(description="Choose the remote computer for calls and computer audio")
    def script_chooseRemoteComputer(self, gesture):
        self.on_choose_peer()

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
        if self.peer_dialog:
            self.peer_dialog.EndModal(wx.ID_CANCEL)
        for item, handler in self.items.values():
            tray.Unbind(wx.EVT_MENU, handler=handler, source=item)
        if self.menu_item:
            tray.toolsMenu.DestroyItem(self.menu_item)
        super().terminate()
