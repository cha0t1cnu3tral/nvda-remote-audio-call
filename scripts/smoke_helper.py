"""Local standard-user smoke checks; no audio files or network transmission.

System capture is opened briefly to check process-loopback startup. Captured
packets are discarded in memory. Nothing is sent to a network or played.
"""
import argparse
import ctypes
from pathlib import Path
import struct
import subprocess
import os


def imports(binary):
    pe = struct.unpack_from("<I", binary, 0x3C)[0]
    if binary[pe:pe+4] != b"PE\0\0":
        raise ValueError("Invalid executable")
    sections = struct.unpack_from("<H", binary, pe+6)[0]
    optional_size = struct.unpack_from("<H", binary, pe+20)[0]
    optional = pe+24
    assert struct.unpack_from("<H", binary, optional)[0] == 0x20B, "Expected x64 helper"
    section_start = optional + optional_size
    def offset(rva):
        for i in range(sections):
            start = section_start + i*40
            virtual_size, virtual, raw_size, raw = struct.unpack_from("<IIII", binary, start+8)
            if virtual <= rva < virtual + max(virtual_size, raw_size):
                return raw + rva - virtual
        raise ValueError("Invalid PE RVA")
    rva = struct.unpack_from("<I", binary, optional+112+8)[0]
    cursor = offset(rva)
    names = []
    while any(binary[cursor:cursor+20]):
        name_rva = struct.unpack_from("<I", binary, cursor+12)[0]
        name_start = offset(name_rva)
        name_end = binary.index(b"\0", name_start)
        names.append(binary[name_start:name_end].decode("ascii"))
        cursor += 20
    return names


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("helper", type=Path)
    parser.add_argument("--loopback", action="store_true")
    parser.add_argument("--call", action="store_true", help="Briefly open microphone and playback; discard microphone packets")
    args = parser.parse_args()
    helper = args.helper.resolve()
    binary = helper.read_bytes()
    assert b"asInvoker" in binary, "Missing standard-user manifest"
    deps = imports(binary)
    assert not any(name.lower().startswith(("vcruntime", "msvcp", "opus")) for name in deps), deps
    print("x64 executable; asInvoker manifest; no separate VC++ or Opus DLL required")
    print("Windows DLL imports:", ", ".join(deps))
    for action in ("--self-test", "--devices"):
        result = subprocess.run([str(helper), action], capture_output=True, timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        if action == "--self-test":
            print(result.stdout.decode().strip())
        else:
            devices = result.stdout.decode("utf-8").splitlines()
            print(f"Audio enumeration passed ({len(devices)} devices; names omitted)")
    print("Current process administrator membership:", bool(ctypes.windll.shell32.IsUserAnAdmin()))
    if args.loopback:
        stop = struct.pack("<I", 1) + b"\x04"
        result = subprocess.run([str(helper), "--capture", "system", "--channels", "2",
                                 "--play", "0", "--nvda-pid", str(os.getpid())],
                                input=stop, capture_output=True, timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        raw = result.stdout
        types = []
        while raw:
            assert len(raw) >= 4
            size = struct.unpack("<I", raw[:4])[0]
            assert 1 <= size <= 4096 and len(raw) >= size+4
            types.append(raw[4])
            if raw[4] == 5:
                raise AssertionError(raw[5:4+size].decode("utf-8", "replace"))
            raw = raw[4+size:]
        assert 6 in types, "Helper never reported ready"
        print("Process-loopback exclusion startup/shutdown passed; packets discarded, no playback")
    if args.call:
        import threading
        import time
        process = subprocess.Popen([str(helper), "--capture", "mic", "--channels", "1", "--play", "1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        types, errors = [], []
        def read():
            while True:
                header = process.stdout.read(4)
                if not header:
                    return
                size = struct.unpack("<I", header)[0]
                data = process.stdout.read(size)
                types.append(data[0])
                if data[0] == 5:
                    errors.append(data[1:].decode("utf-8", "replace"))
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not errors and (6 not in types or types.count(1) < 10):
                time.sleep(.02)
            assert not errors, errors
            assert 6 in types and types.count(1) >= 10, "Call helper did not capture microphone frames"
            process.stdin.write(struct.pack("<I", 1) + b"\x04")
            process.stdin.flush()
            assert process.wait(timeout=5) == 0
            print("Call microphone/playback startup, mono Opus capture and shutdown passed; packets discarded, no network transmission")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            reader.join(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


if __name__ == "__main__":
    main()
