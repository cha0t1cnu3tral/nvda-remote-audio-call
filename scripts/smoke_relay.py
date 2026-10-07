"""Check real relay call signaling and duplex synthetic frames in a private room.

No microphone is opened. NVDA UI services are stubbed; this checks the actual
add-on adapter/engine over TLS, not interactive NVDA on two computers.
"""
import argparse
import json
from pathlib import Path
import select
import socket
import ssl
import threading
import sys
import time
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_adapter import AdapterTests, Transport


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--port", type=int, default=6837)
    parser.add_argument("--native-helper", type=Path, help="Exercise real microphone/codec/playback helpers, muted from startup, with playback volume zero")
    args = parser.parse_args()
    context = ssl.create_default_context()
    sockets, plugins, buffers = [], [], {}
    channel = "rac-smoke-" + uuid.uuid4().hex
    AdapterTests.setUpClass()
    if args.native_helper:
        AdapterTests.plugin_module.NativeAudio.__init__.__globals__["HELPER"] = args.native_helper.resolve()
    try:
        for connection_type in ("master", "slave"):
            connection = context.wrap_socket(socket.create_connection((args.server, args.port), timeout=10), server_hostname=args.server)
            connection.settimeout(5)
            sockets.append(connection)
            buffers[connection] = b""
            for payload in (dict(type="protocol_version", version=2), dict(type="join", channel=channel, connection_type=connection_type)):
                connection.sendall((json.dumps(payload) + "\n").encode())
        identities = [None, None]
        deadline = time.monotonic() + 10
        while None in identities and time.monotonic() < deadline:
            for connection in select.select(sockets, [], [], .1)[0]:
                buffers[connection] += connection.recv(16384)
                while b"\n" in buffers[connection]:
                    line, buffers[connection] = buffers[connection].split(b"\n", 1)
                    message = json.loads(line)
                    index = sockets.index(connection)
                    if message["type"] == "client_joined":
                        identities[index] = message["client"]["id"]
                    elif message["type"] == "channel_joined" and message.get("clients"):
                        identities[index] = message["clients"][0]["id"]
        assert None not in identities, "Relay did not announce both peers"
        for index, role in enumerate(("controlling", "controlled")):
            plugin = AdapterTests.plugin_module.GlobalPlugin.__new__(AdapterTests.plugin_module.GlobalPlugin)
            plugin.closed = plugin.locked = False
            plugin.transport = plugin.session = plugin.native = None
            plugin.old_parse = plugin.hooked_parse = plugin.pair = plugin.menu = plugin.call_dialog = None
            plugin.audio_native = None
            plugin.members = None
            plugin.connection_epoch = 0
            plugin.connection_live = threading.Event()
            plugin.engine = AdapterTests.plugin_module.Engine(plugin)
            plugin.incoming_call = Mock()
            plugin.play = Mock()
            plugin.start_audio = lambda kind, ready, packet, error, stream="call": ready()
            if args.native_helper:
                plugin.settings = dict(input="default", output="default", volume=0)
                def start_audio(kind, ready, packet, error, current=plugin, stream="call"):
                    current.stop_audio()
                    current.native = AdapterTests.plugin_module.NativeAudio(kind, current.settings,
                        ready, packet, error, lambda function, *values: function(*values), initial_muted=True)
                plugin.start_audio = start_audio
                plugin.play = Mock(side_effect=lambda packet, current=plugin, stream="call": current.native.play(packet))
            plugin.supports_system_audio = lambda: True
            transport = Transport()
            session = SimpleNamespace(transport=transport, leaders={} if index == 0 else {identities[index]: {}}, followers={identities[index]: {}} if index == 0 else set())
            plugin._attach(transport, session)
            plugin.engine.connect(role, identities[index])
            plugins.append(plugin)

        def pump(until):
            limit = time.monotonic() + 10
            while time.monotonic() < limit:
                for connection, plugin in zip(sockets, plugins):
                    while not plugin.transport.queue.empty():
                        connection.sendall(plugin.transport.queue.get())
                for connection in select.select(sockets, [], [], .02)[0]:
                    chunk = connection.recv(16384)
                    assert chunk, "Relay disconnected"
                    buffers[connection] += chunk
                    while b"\n" in buffers[connection]:
                        line, buffers[connection] = buffers[connection].split(b"\n", 1)
                        plugins[sockets.index(connection)].transport.parse(line)
                if until():
                    return
            raise AssertionError("Relay call step timed out")

        pump(lambda: all(p.engine.available for p in plugins))
        for caller in (0, 1):
            callee = 1 - caller
            plugins[caller].engine.start_call()
            pump(lambda: plugins[callee].engine.state == "incoming_call")
            plugins[callee].engine.answer()
            pump(lambda: all(p.engine.state == "call" for p in plugins))
            if args.native_helper:
                pump(lambda: all(p.native and p.native.stats[0] >= 10 and p.native.stats[1] >= 10
                    and p.native.stats[2] > 0 for p in plugins))
                print("Native duplex capture, relay, Opus decoding and playback-buffer consumption passed")
            else:
                for index, plugin in enumerate(plugins):
                    plugin.play.reset_mock()
                    plugin.engine._packet(plugin.engine.token, b"synthetic-" + bytes([index]))
                pump(lambda: all(p.play.called for p in plugins))
                for index, plugin in enumerate(plugins):
                    plugin.play.assert_called_once_with(b"synthetic-" + bytes([1-index]), stream="call")
                plugins[1].engine.start_system_audio()
                pump(lambda: all(p.engine.audio.state == "audio" for p in plugins))
                assert all(p.engine.call.state == "call" for p in plugins)
                for plugin in plugins:
                    plugin.play.reset_mock()
                plugins[0].engine.call._packet(plugins[0].engine.call.token, b"controller voice")
                plugins[1].engine.call._packet(plugins[1].engine.call.token, b"controlled voice")
                plugins[1].engine.audio._packet(plugins[1].engine.audio.token, b"computer sound")
                pump(lambda: plugins[0].play.call_count == 2 and plugins[1].play.call_count == 1)
                plugins[0].play.assert_any_call(b"controlled voice", stream="call")
                plugins[0].play.assert_any_call(b"computer sound", stream="audio")
                plugins[1].play.assert_called_once_with(b"controller voice", stream="call")
            plugins[caller].engine.stop()
            pump(lambda: all(p.engine.state == "idle" for p in plugins))
        if args.native_helper:
            print("TLS relay calls and native audio pipeline passed in both directions; microphones muted before capture, playback volume zero")
        else:
            print("TLS relay negotiation, calls in both directions, simultaneous voice/computer-audio synthetic frames and hang-up passed; no microphone opened")
    finally:
        for plugin in plugins:
            plugin._detach()
        for connection in sockets:
            connection.close()
        AdapterTests.tearDownClass()


if __name__ == "__main__":
    main()
