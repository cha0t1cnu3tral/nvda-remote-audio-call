"""Play a quiet synthetic tone and verify native stereo capture/decode/playback.

No microphone, network, or audio recording. Received audio is rendered at volume
zero. --exclude-pid must identify NVDA (or another process outside this test's
process tree), so that the test tone itself is not excluded from capture.
"""
import argparse
import math
from pathlib import Path
import struct
import subprocess
import tempfile
import threading
import time
import wave
import winsound


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("helper", type=Path)
    parser.add_argument("--exclude-pid", required=True, type=int)
    args = parser.parse_args()
    helper = str(args.helper.resolve())
    processes, readers, errors = [], [], []
    ready = [threading.Event(), threading.Event()]
    health = [[], []]

    def start(arguments):
        process = subprocess.Popen([helper, *arguments], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW, bufsize=0)
        processes.append(process)
        return process

    def exact(stream, size):
        data = bytearray()
        while len(data) < size:
            part = stream.read(size-len(data))
            if not part:
                raise EOFError("Helper pipe closed")
            data.extend(part)
        return bytes(data)

    def read(index):
        try:
            while True:
                size = struct.unpack("<I", exact(processes[index].stdout, 4))[0]
                assert 1 <= size <= 4096
                data = exact(processes[index].stdout, size)
                if data[0] == 6:
                    ready[index].set()
                elif data[0] == 5:
                    errors.append(data[1:].decode("utf-8", "replace"))
                elif data[0] == 7:
                    health[index].append(struct.unpack("<IIII", data[1:]))
                elif data[0] == 1 and index == 1:
                    frame = struct.pack("<I", len(data)) + data
                    offset = 0
                    while offset < len(frame):
                        written = processes[0].stdin.write(frame[offset:])
                        if not written:
                            raise OSError("Receiver pipe closed")
                        offset += written
        except (EOFError, OSError):
            pass
        except Exception as exc:
            errors.append(str(exc))

    try:
        start(["--capture", "none", "--channels", "2", "--play", "1", "--volume", "0"])
        start(["--capture", "system", "--channels", "2", "--play", "0",
               "--nvda-pid", str(args.exclude_pid)])
        for index in (0, 1):
            reader = threading.Thread(target=read, args=(index,), daemon=True)
            reader.start()
            readers.append(reader)
        assert all(event.wait(15) for event in ready), errors or "Audio devices never became ready"
        with tempfile.TemporaryDirectory() as directory:
            tone = Path(directory) / "synthetic-tone.wav"
            with wave.open(str(tone), "wb") as output:
                output.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                output.writeframes(b"".join(struct.pack("<hh", sample, sample)
                    for i in range(48000*3)
                    for sample in [int(2000*math.sin(2*math.pi*440*i/48000))]))
            winsound.PlaySound(str(tone), winsound.SND_FILENAME)
        time.sleep(1.2)
        assert not errors, errors
        assert any(captured > 0 and peak > 50 for captured, _, _, peak in health[1]), "System capture produced no non-silent signal"
        assert any(decoded > 0 and rendered > 0 and peak > 50
                   for _, decoded, rendered, peak in health[0]), "Stereo signal did not survive decoding and playback"
        print("Non-silent stereo process capture, Opus encoding/decoding and playback-buffer consumption passed")
        print("No microphone or network used; received playback volume was zero")
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=2)
        for process in processes:
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


if __name__ == "__main__":
    main()
