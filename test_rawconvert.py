"""Tests for rawconvert.py — engines are faked; no external tools required."""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import rawconvert


def capture(func, *args, **kwargs):
    """Run func capturing stdout; return (result, output_text)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = func(*args, **kwargs)
    return result, buf.getvalue()


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)


class TestDiscovery(TempDirTestCase):
    def test_finds_raw_recursively_case_insensitive_and_skips_trash(self):
        (self.root / "sub").mkdir()
        (self.root / rawconvert.TRASH_DIRNAME / "x").mkdir(parents=True)
        for rel in ["a.CR2", "sub/b.cr3", "c.jpg", "notes.txt",
                    rawconvert.TRASH_DIRNAME + "/x/d.cr2"]:
            (self.root / rel).write_bytes(b"x")
        rels = [str(p.relative_to(self.root))
                for p in rawconvert.find_raw_files(self.root)]
        self.assertEqual(rels, ["a.CR2", "sub/b.cr3"])


class TestManifest(TempDirTestCase):
    def test_roundtrip_and_atomic_save(self):
        m = rawconvert.Manifest(self.root)
        m.set("a.CR2", "jpeg", status="converted", src_bytes=10,
              output_relpath="a.jpg", out_bytes=3, engine="sips")
        m.save()

        m2 = rawconvert.Manifest(self.root)
        m2.load()
        row = m2.get("a.CR2", "jpeg")
        self.assertEqual(row["status"], "converted")
        self.assertEqual(row["src_bytes"], "10")
        self.assertEqual(row["output_relpath"], "a.jpg")
        self.assertTrue(row["timestamp"])
        self.assertIsNone(m2.get("a.CR2", "heic"))
        self.assertFalse(list(self.root.glob("*.tmp")),
                         "atomic save must not leave temp files")

    def test_set_rejects_unknown_field(self):
        m = rawconvert.Manifest(self.root)
        with self.assertRaises(KeyError):
            m.set("a.CR2", "jpeg", bogus="x")


class TestEngines(TempDirTestCase):
    def test_image_dimensions_parses_sips_output(self):
        sips_out = b"/path/a.CR3\n  pixelWidth: 6000\n  pixelHeight: 4000\n"
        with mock.patch.object(rawconvert, "run", return_value=(0, sips_out, "")):
            self.assertEqual(
                rawconvert.image_dimensions(self.root / "a.CR3"), (6000, 4000))

    def test_image_dimensions_returns_none_when_unreadable(self):
        with mock.patch.object(rawconvert, "run", return_value=(1, b"", "err")), \
             mock.patch.object(rawconvert, "have_exiftool", return_value=False):
            self.assertIsNone(rawconvert.image_dimensions(self.root / "a.CR3"))


class TestDoctor(unittest.TestCase):
    def test_reports_missing_tools_with_install_hints(self):
        with mock.patch.object(rawconvert, "have_exiftool", return_value=False), \
             mock.patch.object(rawconvert, "dng_converter", return_value=None):
            _, out = capture(rawconvert.cmd_doctor)
        self.assertIn("exiftool.org", out)
        self.assertIn("adobe", out.lower())
        self.assertIn("sips", out)

    def test_reports_present_tools(self):
        with mock.patch.object(rawconvert, "have_exiftool", return_value=True), \
             mock.patch.object(rawconvert, "dng_converter",
                               return_value="/Applications/x"):
            _, out = capture(rawconvert.cmd_doctor)
        self.assertNotIn("exiftool.org", out)


class TestScan(TempDirTestCase):
    def test_scan_reports_counts_and_sizes(self):
        (self.root / "a.CR2").write_bytes(b"x" * 1000)
        (self.root / "b.cr3").write_bytes(b"x" * 3000)
        (self.root / "ignore.jpg").write_bytes(b"x" * 500)
        _, out = capture(rawconvert.cmd_scan, self.root)
        self.assertIn("2 RAW files", out)
        self.assertIn("3.9 KB", out)  # 4000 bytes / 1024


if __name__ == "__main__":
    unittest.main()
