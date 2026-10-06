"""Build a reproducible NVDA archive; refuses to package without the helper."""
import argparse
import hashlib
from pathlib import Path
import re
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--helper", type=Path, required=True)
    args = parser.parse_args()
    if not args.helper.is_file() or args.helper.read_bytes()[:2] != b"MZ":
        parser.error("Supply the compiled Windows helper executable")
    addon = ROOT / "addon"
    dest = addon / "globalPlugins" / "remoteAudioCall" / "bin" / "remoteAudioHelper.exe"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.helper, dest)
    shutil.copyfile(ROOT / "LICENSE", addon / "LICENSE")
    shutil.copyfile(ROOT / "THIRD_PARTY_NOTICES.md", addon / "THIRD_PARTY_NOTICES.md")
    version = re.search(r"^version\s*=\s*(\S+)", (addon / "manifest.ini").read_text(), re.M)[1]
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    package = dist / f"remoteAudioCall-{version}.nvda-addon"
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source in sorted(addon.rglob("*")):
            if not source.is_file() or "__pycache__" in source.parts or source.suffix == ".pyc":
                continue
            relative = source.relative_to(addon).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, source.read_bytes())
    checksum = hashlib.sha256(package.read_bytes()).hexdigest()
    (dist / "SHA256SUMS.txt").write_text(f"{checksum}  {package.name}\n", encoding="ascii")
    with zipfile.ZipFile(package) as archive:
        assert archive.testzip() is None
        assert "manifest.ini" in archive.namelist()
        assert "globalPlugins/remoteAudioCall/bin/remoteAudioHelper.exe" in archive.namelist()
    print(package)


if __name__ == "__main__":
    main()

