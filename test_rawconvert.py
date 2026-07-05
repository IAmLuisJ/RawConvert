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


def fake_engine_write(src, dst, *args, **kwargs):
    """Stand-in for a conversion engine: writes bytes to the target path."""
    Path(dst).write_bytes(b"FAKEIMG")


class ConvertTestCase(TempDirTestCase):
    """Base: patches all real engines out of cmd_convert."""

    def setUp(self):
        super().setUp()
        for name in ("sips_convert", "dng_convert"):
            patcher = mock.patch.object(rawconvert, name,
                                        side_effect=fake_engine_write)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(rawconvert, "extract_embedded_jpeg",
                                    return_value=False)
        self.extract_embedded_jpeg = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(rawconvert, "copy_metadata")
        self.copy_metadata = patcher.start()
        self.addCleanup(patcher.stop)

    def make_raws(self, *rels):
        for rel in rels:
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"RAWDATA-" + rel.encode())

    def manifest(self):
        m = rawconvert.Manifest(self.root)
        m.load()
        return m


class TestConvert(ConvertTestCase):
    def test_converts_all_and_records_manifest(self):
        self.make_raws("a.CR2", "sub/b.cr3")
        counts, _ = capture(rawconvert.cmd_convert, self.root, "jpeg")
        self.assertEqual(counts["converted"], 2)
        self.assertTrue((self.root / "a.jpg").exists())
        self.assertTrue((self.root / "sub/b.jpg").exists())
        self.assertFalse(list(self.root.rglob("*.partial")))
        row = self.manifest().get("a.CR2", "jpeg")
        self.assertEqual(row["status"], "converted")
        self.assertEqual(row["engine"], "sips")
        self.assertEqual(row["out_bytes"], str(len(b"FAKEIMG")))
        self.assertTrue(int(row["src_bytes"]) > 0)

    def test_jpeg_prefers_embedded_extraction(self):
        self.make_raws("a.CR2")
        self.extract_embedded_jpeg.side_effect = (
            lambda src, dst: (fake_engine_write(src, dst), True)[1])
        counts, _ = capture(rawconvert.cmd_convert, self.root, "jpeg")
        self.assertEqual(counts["converted"], 1)
        self.sips_convert.assert_not_called()
        self.assertEqual(self.manifest().get("a.CR2", "jpeg")["engine"],
                         "exiftool-embedded")

    def test_rerun_skips_already_converted(self):
        self.make_raws("a.CR2")
        capture(rawconvert.cmd_convert, self.root, "heic")
        first_calls = self.sips_convert.call_count
        counts, _ = capture(rawconvert.cmd_convert, self.root, "heic")
        self.assertEqual(self.sips_convert.call_count, first_calls)
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(counts["converted"], 0)

    def test_collision_is_not_overwritten(self):
        self.make_raws("a.CR2")
        (self.root / "a.jpg").write_bytes(b"PRECIOUS")
        counts, _ = capture(rawconvert.cmd_convert, self.root, "jpeg")
        self.assertEqual(counts["collision"], 1)
        self.assertEqual((self.root / "a.jpg").read_bytes(), b"PRECIOUS")
        self.assertEqual(self.manifest().get("a.CR2", "jpeg")["status"],
                         "collision")

    def test_sample_limits_file_count(self):
        self.make_raws("a.CR2", "b.CR2", "c.CR2")
        counts, _ = capture(rawconvert.cmd_convert, self.root, "heic",
                            sample=2)
        self.assertEqual(counts["converted"], 2)

    def test_dry_run_changes_nothing(self):
        self.make_raws("a.CR2")
        counts, out = capture(rawconvert.cmd_convert, self.root, "jpeg",
                              dry_run=True)
        self.assertIn("a.CR2", out)
        self.assertFalse((self.root / "a.jpg").exists())
        self.assertFalse((self.root / rawconvert.MANIFEST_NAME).exists())

    def test_engine_failure_recorded_and_batch_continues(self):
        self.make_raws("a.CR2", "b.CR2")

        def explode_on_a(src, dst, *args, **kwargs):
            if Path(src).name == "a.CR2":
                raise rawconvert.EngineError("boom")
            fake_engine_write(src, dst)

        self.sips_convert.side_effect = explode_on_a
        counts, _ = capture(rawconvert.cmd_convert, self.root, "heic")
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["converted"], 1)
        self.assertFalse(list(self.root.rglob("*.partial")))
        row = self.manifest().get("a.CR2", "heic")
        self.assertEqual(row["status"], "failed")
        self.assertIn("boom", row["note"])


if __name__ == "__main__":
    unittest.main()
