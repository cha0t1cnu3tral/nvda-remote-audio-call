"""Exercise three real relay participants, selection and reconnects without capture."""
import argparse
import json
from pathlib import Path
import select
import socket
import ssl
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
    parser.add_argument("--observer", choices=("plain", "addon"), default="plain")
    args = parser.parse_args()
    context = ssl.create_default_context()
    channel = "rac-peers-" + uuid.uuid4().hex
    connections, plugins, buffers = [], [], {}
    AdapterTests.setUpClass()
    module = AdapterTests.plugin_module

    def join(index):
        connection = context.wrap_socket(socket.create_connection((args.server, args.port), timeout=10), server_hostname=args.server)
        connection.settimeout(5)
        buffers[connection] = b""
        role = "slave" if index == 1 else "master"
        for payload in (dict(type="protocol_version", version=2), dict(type="join", channel=channel, connection_type=role)):
            connection.sendall((json.dumps(payload) + "\n").encode())
        return connection

    def make_plugin(index):
        plugin = module.GlobalPlugin.__new__(module.GlobalPlugin)
        plugin.closed = plugin.locked = False
        plugin.transport = plugin.session = plugin.native = plugin.audio_native = None
        plugin.old_parse = plugin.hooked_parse = plugin.pair = plugin.menu = plugin.call_dialog = None
        plugin._init_connection_state()
        plugin.engine = module.Engine(plugin)
        plugin.incoming_call = Mock()
        plugin.play = Mock()
        plugin.start_audio = lambda kind, ready, packet, error, stream="call": ready()
        plugin.supports_system_audio = lambda: True
        transport = Transport()
        session = SimpleNamespace(transport=transport, leaders={}, followers={})
        plugin._attach(transport, session, "controlled" if index == 1 else "controlling")
        return plugin

    def pump(until):
        limit = time.monotonic() + 12
        while time.monotonic() < limit:
            for connection, plugin in zip(connections, plugins):
                if plugin:
                    plugin._reconcile_peer()
                    plugin.engine.tick()
                    if time.monotonic() - plugin.last_hello >= 2:
                        plugin._discover()
                        plugin.last_hello = time.monotonic()
                    while not plugin.transport.queue.empty():
                        connection.sendall(plugin.transport.queue.get())
            for connection in select.select(connections, [], [], .02)[0]:
                chunk = connection.recv(16384)
                assert chunk, "Unexpected relay disconnect"
                buffers[connection] += chunk
                while b"\n" in buffers[connection]:
                    line, buffers[connection] = buffers[connection].split(b"\n", 1)
                    plugin = plugins[connections.index(connection)]
                    if plugin:
                        plugin.transport.parse(line)
            if until():
                return
        raise AssertionError("Three-participant relay step timed out")

    def peer_id(receiver, sender):
        return next(peer for peer, info in receiver.peers.items() if info["instance"] == sender.engine.instance)

    def call(caller=0):
        callee = 1-caller
        plugins[caller].engine.start_call()
        pump(lambda: plugins[callee].engine.state == "incoming_call")
        plugins[callee].engine.answer()
        pump(lambda: all(plugin.engine.call.state == "call" for plugin in plugins[:2]))

    try:
        for index in range(3):
            connections.append(join(index))
            plugins.append(make_plugin(index) if index < 2 or args.observer == "addon" else None)
        pump(lambda: len(plugins[0].peers) == 1 and len(plugins[1].peers) == (2 if plugins[2] else 1))
        for receiver, sender in ((plugins[0], plugins[1]), (plugins[1], plugins[0])):
            assert receiver.select_peer(peer_id(receiver, sender), announce=False)
        call()
        assert all(plugin.engine.available for plugin in plugins[:2])
        plugins[1].engine.start_system_audio()
        pump(lambda: all(plugin.engine.audio.state == "audio" for plugin in plugins[:2]))
        plugins[0].engine.call._packet(plugins[0].engine.call.token, b"controller voice")
        plugins[1].engine.call._packet(plugins[1].engine.call.token, b"controlled voice")
        plugins[1].engine.audio._packet(plugins[1].engine.audio.token, b"computer sound")
        pump(lambda: plugins[0].play.call_count == 2 and plugins[1].play.call_count == 1)
        plugins[0].play.assert_any_call(b"controlled voice", stream="call")
        plugins[0].play.assert_any_call(b"computer sound", stream="audio")
        if plugins[2]:
            plugins[2].incoming_call.assert_not_called()
            plugins[2].play.assert_not_called()
            # Another compatible caller gets a busy response without disturbing
            # the pair already exchanging voice and computer audio.
            assert plugins[2].select_peer(peer_id(plugins[2], plugins[1]), announce=False)
            plugins[2].engine.start_call()
            pump(lambda: plugins[2].engine.state == "idle")
            assert plugins[1].engine.call.state == "call"
            assert plugins[1].engine.audio.state == "audio"
        # An unrelated observer can leave and rejoin while both streams keep
        # running. The plain observer has no plugin to participate in discovery.
        old_observer_id = next(iter(plugins[0]._participants()[0]))
        old_observer = connections[2]
        if plugins[2]:
            plugins[2].transport.connected = False
            for callback in list(plugins[2].transport.transportDisconnected.handlers):
                callback()
        old_observer.close()
        buffers.pop(old_observer)
        connections[2] = join(2)
        if plugins[2]:
            plugins[2].transport.connected = True
            for callback in list(plugins[2].transport.transportConnected.handlers):
                callback()
        pump(lambda: old_observer_id not in plugins[0]._participants()[0] and len(plugins[0]._participants()[0]) == 1)
        assert all(plugin.engine.call.state == "call" and plugin.engine.audio.state == "audio" and plugin.engine.available for plugin in plugins[:2])
        # Lose the selected controller's network connection during both streams,
        # then reconnect the same plugin/transport with a newly allocated ID.
        call_token = plugins[0].engine.call.token
        plugins[0].engine.toggle_mute()
        old_id = plugins[1].engine.peer
        old_connection = connections[0]
        plugins[0].transport.connected = False
        for callback in list(plugins[0].transport.transportDisconnected.handlers):
            callback()
        old_connection.close()
        buffers.pop(old_connection)
        connections[0] = join(0)
        plugins[0].transport.connected = True
        for callback in list(plugins[0].transport.transportConnected.handlers):
            callback()
        pump(lambda: all(plugin.engine.available for plugin in plugins[:2]) and plugins[1].engine.peer != old_id)
        pump(lambda: all(plugin.engine.call.state == "call" for plugin in plugins[:2]))
        assert all(plugin.engine.call.token == call_token and plugin.engine.audio.state == "idle" for plugin in plugins[:2])
        assert plugins[0].engine.muted
        for plugin in plugins[:2]:
            plugin.play.reset_mock()
        plugins[0].engine.call._packet(call_token, b"recovered controller voice")
        plugins[1].engine.call._packet(call_token, b"recovered controlled voice")
        pump(lambda: all(plugin.play.called for plugin in plugins[:2]))
        plugins[0].play.assert_called_once_with(b"recovered controlled voice", stream="call")
        plugins[1].play.assert_called_once_with(b"recovered controller voice", stream="call")
        plugins[0].engine.stop()
        pump(lambda: all(plugin.engine.state == "idle" for plugin in plugins[:2]))
        call(caller=1)
        plugins[1].engine.stop()
        pump(lambda: all(plugin.engine.state == "idle" for plugin in plugins[:2]))
        print("Three-participant TLS relay passed: " + args.observer + " observer join/leave, addressed simultaneous audio, automatic call recovery with mute and duplex frames, and calls in both directions; no capture opened")
    finally:
        for plugin in plugins:
            if plugin:
                plugin._detach()
        for connection in connections:
            connection.close()
        AdapterTests.tearDownClass()


if __name__ == "__main__":
    main()
