"""Bounded, asynchronous IPC to the standard-user native helper."""
import os
from pathlib import Path
import queue
import struct
import subprocess
import threading

HELPER = Path(__file__).parent / "bin" / "remoteAudioHelper.exe"
HEADER = struct.Struct("<I")


def enumerate_devices():
    result = subprocess.run([str(HELPER), "--devices"], capture_output=True,
                            creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip() or "Cannot list audio devices")
    devices = {"input": [("default", "Windows default microphone")],
               "output": [("default", "Windows default playback device")]}
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        fields = line.split("\t", 2)
        if len(fields) == 3 and fields[0] in devices:
            devices[fields[0]].append((fields[1], fields[2]))
    return devices


class NativeAudio:
    def __init__(self, kind, settings, ready, packet, error, dispatch):
        self.ready, self.packet, self.error, self.dispatch = ready, packet, error, dispatch
        self.active = threading.Event()
        self.active.set()
        self.media = queue.Queue(maxsize=6)
        self.controls = queue.Queue()
        capture, channels, play = {"call": ("mic", 1, True),
                                   "send": ("system", 2, False),
                                   "receive": ("none", 2, True)}[kind]
        self.process = subprocess.Popen([
            str(HELPER), "--capture", capture, "--channels", str(channels),
            "--play", str(int(play)), "--nvda-pid", str(os.getpid()),
            "--input", settings["input"], "--output", settings["output"],
            "--volume", str(settings["volume"])
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW, bufsize=0)
        threading.Thread(target=self._read, daemon=True, name="RemoteAudioRead").start()
        threading.Thread(target=self._write, daemon=True, name="RemoteAudioWrite").start()

    def _fail(self, message):
        if self.active.is_set():
            self.dispatch(self.error, message)

    def _exact(self, count):
        chunks = bytearray()
        while len(chunks) < count:
            part = self.process.stdout.read(count - len(chunks))
            if not part:
                raise EOFError
            chunks.extend(part)
        return bytes(chunks)

    def _read(self):
        try:
            while self.active.is_set():
                size = HEADER.unpack(self._exact(4))[0]
                if not 1 <= size <= 4096:
                    raise ValueError("Invalid helper response")
                data = self._exact(size)
                if not self.active.is_set():
                    break
                if data[0] == 1 and len(data) <= 1276:
                    self.packet(data[1:])
                elif data[0] == 6:
                    self.dispatch(self.ready)
                elif data[0] == 5:
                    self._fail(data[1:].decode("utf-8", "replace"))
                    break
        except (EOFError, OSError, ValueError):
            self._fail("The audio helper stopped unexpectedly")

    def _write(self):
        try:
            while self.active.is_set():
                try:
                    data = self.controls.get_nowait()
                except queue.Empty:
                    try:
                        data = self.media.get(timeout=0.02)
                    except queue.Empty:
                        continue
                if not self.active.is_set():
                    break
                frame = HEADER.pack(len(data)) + data
                # FileIO can return a short write. Complete frames before the next one.
                offset = 0
                while offset < len(frame):
                    count = self.process.stdin.write(frame[offset:])
                    if not count:
                        raise OSError("Helper pipe closed")
                    offset += count
        except (OSError, ValueError):
            self._fail("Cannot communicate with the audio helper")

    def play(self, packet):
        if not self.active.is_set():
            return
        try:
            self.media.put_nowait(b"\x01" + packet)
        except queue.Full:
            try:
                self.media.get_nowait()
            except queue.Empty:
                pass
            try:
                self.media.put_nowait(b"\x01" + packet)
            except queue.Full:
                pass

    def mute(self, muted):
        if self.active.is_set():
            self.controls.put(b"\x02" + bytes([int(muted)]))

    def set_volume(self, volume):
        if self.active.is_set():
            self.controls.put(b"\x03" + bytes([volume]))

    def stop(self):
        if not self.active.is_set():
            return
        self.active.clear()
        # Killing our own standard-user child closes devices promptly even if a pipe
        # or Windows audio activation is blocked. Reaping/closing happens off the UI thread.
        try:
            self.process.kill()
        except OSError:
            pass
        def reap():
            self.process.wait()
            for stream in (self.process.stdin, self.process.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
        threading.Thread(target=reap, daemon=True, name="RemoteAudioCleanup").start()

