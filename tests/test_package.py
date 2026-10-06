import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location("audio_package", Path(__file__).resolve().parents[1] / "scripts/package.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class PackageTests(unittest.TestCase):
    def test_package_layout_checksum_and_reproducibility(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            addon = root / "addon"
            addon.mkdir()
            (addon / "manifest.ini").write_text("version = 0.1.0\n")
            (root / "LICENSE").write_text("license")
            (root / "THIRD_PARTY_NOTICES.md").write_text("notices")
            cache = addon / "__pycache__"
            cache.mkdir()
            (cache / "bad.pyc").write_bytes(b"not source")
            helper = root / "helper.exe"
            helper.write_bytes(b"MZtest executable fixture")
            with patch.object(package, "ROOT", root), patch("sys.argv", ["package.py", "--helper", str(helper)]):
                package.main()
                archive = root / "dist/remoteAudioCall-0.1.0.nvda-addon"
                first = archive.read_bytes()
                package.main()
                self.assertEqual(first, archive.read_bytes())
            with zipfile.ZipFile(archive) as z:
                self.assertIn("manifest.ini", z.namelist())
                self.assertIn("globalPlugins/remoteAudioCall/bin/remoteAudioHelper.exe", z.namelist())
                self.assertNotIn("__pycache__/bad.pyc", z.namelist())
                self.assertIsNone(z.testzip())
            self.assertIn("remoteAudioCall-0.1.0.nvda-addon", (root / "dist/SHA256SUMS.txt").read_text())

    def test_missing_helper_is_not_packaged(self):
        with patch("sys.argv", ["package.py", "--helper", "does-not-exist.exe"]):
            with self.assertRaises(SystemExit):
                package.main()

