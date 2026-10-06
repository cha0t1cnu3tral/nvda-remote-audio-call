import importlib.util
from pathlib import Path
import queue
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("audio_native", Path(__file__).resolve().parents[1] / "addon/globalPlugins/remoteAudioCall/native.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class NativeTests(unittest.TestCase):
    def test_media_queue_discards_oldest_frame(self):
        audio = native.NativeAudio.__new__(native.NativeAudio)
        audio.active = threading.Event()
        audio.active.set()
        audio.media = queue.Queue(maxsize=6)
        for i in range(50):
            audio.play(bytes([i]))
        self.assertEqual(audio.media.qsize(), 6)
        packets = [audio.media.get_nowait() for _ in range(6)]
        self.assertEqual(packets, [b"\x01" + bytes([i]) for i in range(44, 50)])

    def test_stopped_helper_does_not_accept_playback(self):
        audio = native.NativeAudio.__new__(native.NativeAudio)
        audio.active = threading.Event()
        audio.media = queue.Queue(maxsize=6)
        audio.play(b"old stream")
        self.assertTrue(audio.media.empty())

    def test_microphone_mute_has_priority_over_media(self):
        audio = native.NativeAudio.__new__(native.NativeAudio)
        audio.active = threading.Event()
        audio.active.set()
        audio.media = queue.Queue(maxsize=6)
        audio.controls = queue.Queue()
        audio.play(b"voice")
        audio.mute(True)
        audio.set_volume(25)
        self.assertEqual(audio.controls.get_nowait(), b"\x02\x01")
        self.assertEqual(audio.controls.get_nowait(), b"\x03\x19")

    def test_enumeration_uses_stable_ids_and_unicode_names(self):
        result = type("Result", (), {"returncode": 0, "stderr": b"", "stdout": "input\tdevice-123\tMic é\noutput\tdevice-456\tSpeaker\nmalformed\n".encode()})()
        with patch.object(native.subprocess, "CREATE_NO_WINDOW", 0, create=True), patch.object(native.subprocess, "run", return_value=result):
            devices = native.enumerate_devices()
        self.assertEqual(devices["input"][0][0], "default")
        self.assertEqual(devices["input"][1], ("device-123", "Mic é"))
        self.assertEqual(devices["output"][1][0], "device-456")

