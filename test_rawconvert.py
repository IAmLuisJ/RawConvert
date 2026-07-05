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

    def test_skips_hidden_and_appledouble_files(self):
        # macOS writes ._* AppleDouble sidecars on FAT/exFAT drives; they have
        # a .CR3 extension but are metadata, not photos
        for rel in ["._a.CR3", ".hidden.CR2", "real.CR3"]:
            (self.root / rel).write_bytes(b"x")
        rels = [str(p.relative_to(self.root))
                for p in rawconvert.find_raw_files(self.root)]
        self.assertEqual(rels, ["real.CR3"])

    def test_no_recurse_finds_only_top_level(self):
        (self.root / "sub").mkdir()
        (self.root / "a.CR2").write_bytes(b"x")
        (self.root / "sub/b.cr3").write_bytes(b"x")
        rels = [str(p.relative_to(self.root))
                for p in rawconvert.find_raw_files(self.root, recurse=False)]
        self.assertEqual(rels, ["a.CR2"])


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

    def test_no_recurse_converts_only_top_level(self):
        self.make_raws("a.CR2", "sub/b.cr3")
        counts, _ = capture(rawconvert.cmd_convert, self.root, "jpeg",
                            recurse=False)
        self.assertEqual(counts["converted"], 1)
        self.assertTrue((self.root / "a.jpg").exists())
        self.assertFalse((self.root / "sub/b.jpg").exists())

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


class TestVerify(TempDirTestCase):
    def seed(self, fmt="jpeg", out_name="a.jpg", out_bytes=b"JPEGDATA"):
        (self.root / "a.CR2").write_bytes(b"RAW")
        if out_bytes is not None:
            (self.root / out_name).write_bytes(out_bytes)
        m = rawconvert.Manifest(self.root)
        m.set("a.CR2", fmt, output_relpath=out_name, status="converted",
              src_bytes=3, out_bytes=len(out_bytes or b""), engine="test")
        m.save()

    def run_verify(self, fmt, dims):
        """dims: maps filename -> (w, h) or None."""
        def fake_dims(path):
            return dims.get(Path(path).name)
        with mock.patch.object(rawconvert, "image_dimensions",
                               side_effect=fake_dims):
            counts, _ = capture(rawconvert.cmd_verify, self.root, fmt)
        m = rawconvert.Manifest(self.root)
        m.load()
        return counts, m

    def test_matching_dimensions_verified(self):
        self.seed()
        counts, m = self.run_verify(
            "jpeg", {"a.CR2": (6000, 4000), "a.jpg": (6000, 4000)})
        self.assertEqual(counts["verified"], 1)
        self.assertEqual(m.get("a.CR2", "jpeg")["status"], "verified")

    def test_swapped_dimensions_verified(self):
        self.seed()
        counts, _ = self.run_verify(
            "jpeg", {"a.CR2": (6000, 4000), "a.jpg": (4000, 6000)})
        self.assertEqual(counts["verified"], 1)

    def test_near_dimensions_verified(self):
        # Canon sensor dims can differ from rendered dims by a few pixels
        self.seed()
        counts, _ = self.run_verify(
            "jpeg", {"a.CR2": (6024, 4020), "a.jpg": (6000, 4000)})
        self.assertEqual(counts["verified"], 1)

    def test_dimension_mismatch_fails(self):
        self.seed()
        counts, m = self.run_verify(
            "jpeg", {"a.CR2": (6000, 4000), "a.jpg": (3000, 2000)})
        self.assertEqual(counts["failed"], 1)
        row = m.get("a.CR2", "jpeg")
        self.assertEqual(row["status"], "failed")
        self.assertIn("dimension", row["note"])

    def test_missing_output_fails(self):
        self.seed(out_bytes=None)
        counts, m = self.run_verify("jpeg", {"a.CR2": (6000, 4000)})
        self.assertEqual(counts["failed"], 1)
        self.assertIn("missing", m.get("a.CR2", "jpeg")["note"])

    def test_zero_byte_output_fails(self):
        self.seed(out_bytes=b"")
        counts, _ = self.run_verify(
            "jpeg", {"a.CR2": (6000, 4000), "a.jpg": (6000, 4000)})
        self.assertEqual(counts["failed"], 1)

    def test_dng_with_tiff_magic_and_no_dims_verified(self):
        self.seed(fmt="dng", out_name="a.dng", out_bytes=b"II*\x00rest")
        counts, _ = self.run_verify("dng", {"a.CR2": (6000, 4000),
                                            "a.dng": None})
        self.assertEqual(counts["verified"], 1)

    def test_dng_with_bad_magic_fails(self):
        self.seed(fmt="dng", out_name="a.dng", out_bytes=b"NOTATIFF")
        counts, _ = self.run_verify("dng", {"a.CR2": (6000, 4000),
                                            "a.dng": None})
        self.assertEqual(counts["failed"], 1)


class TestFailureClassification(unittest.TestCase):
    def test_appledouble_classified(self):
        info = rawconvert.classify_failure(
            Path("._a.CR3"), 4096, "sips failed: Cannot extract image")
        self.assertEqual(info["code"], "RC01")

    def test_tiny_file_classified_as_not_raw(self):
        info = rawconvert.classify_failure(
            Path("a.CR3"), 900, "sips failed: Cannot extract image")
        self.assertEqual(info["code"], "RC02")

    def test_unsupported_or_corrupt(self):
        info = rawconvert.classify_failure(
            Path("a.CR3"), 25 * 1024 * 1024,
            "sips failed: Error: Cannot extract image from file.")
        self.assertEqual(info["code"], "RC03")
        self.assertTrue(info["steps"])

    def test_disk_full(self):
        info = rawconvert.classify_failure(
            Path("a.CR3"), 25 * 1024 * 1024, "No space left on device")
        self.assertEqual(info["code"], "RC05")

    def test_permission_denied(self):
        info = rawconvert.classify_failure(
            Path("a.CR3"), 25 * 1024 * 1024, "Permission denied")
        self.assertEqual(info["code"], "RC06")

    def test_unknown_fallback(self):
        info = rawconvert.classify_failure(
            Path("a.CR3"), 25 * 1024 * 1024, "something inexplicable")
        self.assertEqual(info["code"], "RC99")


class TestErrorLog(ConvertTestCase):
    def test_failed_conversion_writes_error_log_with_debug_steps(self):
        self.make_raws("a.CR2", "b.CR2")
        # big enough to pass the too-small (RC02) gate and hit RC03
        (self.root / "a.CR2").write_bytes(
            b"\0" * (rawconvert.MIN_PLAUSIBLE_RAW_BYTES + 1))

        def explode_on_a(src, dst, *args, **kwargs):
            if Path(src).name == "a.CR2":
                raise rawconvert.EngineError(
                    "sips failed: Error: Cannot extract image from file.")
            fake_engine_write(src, dst)

        self.sips_convert.side_effect = explode_on_a
        counts, out = capture(rawconvert.cmd_convert, self.root, "heic")
        self.assertEqual(counts["failed"], 1)

        log = self.root / rawconvert.ERROR_LOG_NAME
        self.assertTrue(log.exists())
        text = log.read_text()
        self.assertIn("RC03", text)
        self.assertIn("a.CR2", text)
        self.assertIn("debug steps", text.lower())
        self.assertIn("Cannot extract image", text)
        # manifest note carries the code; console points at the log
        self.assertIn("[RC03]",
                      self.manifest().get("a.CR2", "heic")["note"])
        self.assertIn(rawconvert.ERROR_LOG_NAME, out)

    def test_no_error_log_when_nothing_fails(self):
        self.make_raws("a.CR2")
        capture(rawconvert.cmd_convert, self.root, "heic")
        self.assertFalse((self.root / rawconvert.ERROR_LOG_NAME).exists())


class TestConvertToOutputFolder(ConvertTestCase):
    def setUp(self):
        super().setUp()
        self.out_tmp = tempfile.TemporaryDirectory()
        self.out_root = Path(self.out_tmp.name)
        self.addCleanup(self.out_tmp.cleanup)

    def test_mirrors_structure_into_output_root(self):
        self.make_raws("a.CR2", "sub/deep/b.cr3")
        counts, _ = capture(rawconvert.cmd_convert, self.root, "jpeg",
                            output_root=self.out_root)
        self.assertEqual(counts["converted"], 2)
        self.assertTrue((self.out_root / "a.jpg").exists())
        self.assertTrue((self.out_root / "sub/deep/b.jpg").exists())
        # nothing written next to the sources
        self.assertFalse((self.root / "a.jpg").exists())
        row = self.manifest().get("a.CR2", "jpeg")
        self.assertEqual(row["output_root"], str(self.out_root.resolve()))
        self.assertEqual(row["output_relpath"], "a.jpg")

    def test_rerun_with_same_output_skips(self):
        self.make_raws("a.CR2")
        capture(rawconvert.cmd_convert, self.root, "heic",
                output_root=self.out_root)
        counts, _ = capture(rawconvert.cmd_convert, self.root, "heic",
                            output_root=self.out_root)
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(counts["converted"], 0)

    def test_collision_in_output_root_not_overwritten(self):
        self.make_raws("a.CR2")
        (self.out_root / "a.jpg").write_bytes(b"PRECIOUS")
        counts, _ = capture(rawconvert.cmd_convert, self.root, "jpeg",
                            output_root=self.out_root)
        self.assertEqual(counts["collision"], 1)
        self.assertEqual((self.out_root / "a.jpg").read_bytes(), b"PRECIOUS")

    def test_dry_run_creates_no_output_dirs(self):
        self.make_raws("sub/a.CR2")
        capture(rawconvert.cmd_convert, self.root, "jpeg",
                output_root=self.out_root, dry_run=True)
        self.assertFalse((self.out_root / "sub").exists())

    def test_verify_uses_recorded_output_root(self):
        self.make_raws("a.CR2")
        capture(rawconvert.cmd_convert, self.root, "jpeg",
                output_root=self.out_root)

        def fake_dims(path):
            return (6000, 4000)
        with mock.patch.object(rawconvert, "image_dimensions",
                               side_effect=fake_dims):
            counts, _ = capture(rawconvert.cmd_verify, self.root, "jpeg")
        self.assertEqual(counts["verified"], 1)

    def test_cleanup_stages_rejected_output_on_its_own_drive(self):
        self.make_raws("a.CR2")
        capture(rawconvert.cmd_convert, self.root, "jpeg",
                output_root=self.out_root)   # will be the rejected format
        capture(rawconvert.cmd_convert, self.root, "heic")  # kept, in-place
        m = rawconvert.Manifest(self.root)
        m.load()
        m.set("a.CR2", "heic", status="verified")
        m.save()

        counts, _ = capture(rawconvert.cmd_cleanup, self.root, "heic")
        # original staged in the source root's trash
        self.assertTrue((self.root / rawconvert.TRASH_DIRNAME /
                         "originals" / "a.CR2").exists())
        # rejected jpeg staged in a trash on the OUTPUT root, not the source
        self.assertFalse((self.out_root / "a.jpg").exists())
        self.assertTrue((self.out_root / rawconvert.TRASH_DIRNAME /
                         "rejected" / "a.jpg").exists())
        self.assertEqual(counts["originals_staged"], 1)
        self.assertEqual(counts["rejected_staged"], 1)


class TestManifestBackwardCompat(TempDirTestCase):
    def test_loads_manifest_written_without_output_root_column(self):
        old_fields = [f for f in rawconvert.MANIFEST_FIELDS
                      if f != "output_root"]
        import csv
        with open(self.root / rawconvert.MANIFEST_NAME, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=old_fields)
            w.writeheader()
            w.writerow({"source_relpath": "a.CR2", "format": "jpeg",
                        "output_relpath": "a.jpg", "src_bytes": "10",
                        "out_bytes": "3", "engine": "sips",
                        "status": "converted", "timestamp": "t", "note": ""})
        m = rawconvert.Manifest(self.root)
        m.load()
        row = m.get("a.CR2", "jpeg")
        self.assertEqual(row["output_root"], "")
        m.save()  # must not raise


class TestCompare(ConvertTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(rawconvert, "open_in_preview")
        self.open_in_preview = patcher.start()
        self.addCleanup(patcher.stop)
        self.out_dir = self.root / "cmp"
        self.out_dir.mkdir()

    def test_converts_to_all_formats_and_opens_preview(self):
        self.make_raws("a.CR3")
        with mock.patch.object(rawconvert, "dng_converter",
                               return_value="/fake/converter"):
            results, out = capture(rawconvert.cmd_compare,
                                   self.root / "a.CR3",
                                   out_dir=self.out_dir)
        self.assertEqual(sorted(results), ["dng", "heic", "jpeg"])
        for path in results.values():
            self.assertTrue(path.exists())
        opened = self.open_in_preview.call_args[0][0]
        self.assertEqual(len(opened), 3)
        self.assertIn("% of RAW", out)

    def test_dng_skipped_when_converter_missing(self):
        self.make_raws("a.CR3")
        with mock.patch.object(rawconvert, "dng_converter",
                               return_value=None):
            results, out = capture(rawconvert.cmd_compare,
                                   self.root / "a.CR3",
                                   out_dir=self.out_dir)
        self.assertEqual(sorted(results), ["heic", "jpeg"])
        self.assertIn("skipped", out)
        self.assertEqual(len(self.open_in_preview.call_args[0][0]), 2)

    def test_one_format_failing_does_not_block_others(self):
        self.make_raws("a.CR3")

        def heic_explodes(src, dst, fmt, quality):
            if fmt == "heic":
                raise rawconvert.EngineError("boom")
            fake_engine_write(src, dst)

        self.sips_convert.side_effect = heic_explodes
        with mock.patch.object(rawconvert, "dng_converter",
                               return_value=None):
            results, out = capture(rawconvert.cmd_compare,
                                   self.root / "a.CR3",
                                   out_dir=self.out_dir)
        self.assertEqual(sorted(results), ["jpeg"])
        self.assertIn("FAILED", out)
        self.assertEqual(len(self.open_in_preview.call_args[0][0]), 1)

    def test_rejects_non_raw_file(self):
        (self.root / "x.jpg").write_bytes(b"j")
        with self.assertRaises(SystemExit):
            capture(rawconvert.cmd_compare, self.root / "x.jpg",
                    out_dir=self.out_dir)


class TestStatus(TempDirTestCase):
    def test_status_reports_per_format_sizes(self):
        m = rawconvert.Manifest(self.root)
        m.set("a.CR2", "jpeg", output_relpath="a.jpg", status="verified",
              src_bytes=1000, out_bytes=250, engine="sips")
        m.set("b.CR2", "jpeg", output_relpath="b.jpg", status="converted",
              src_bytes=1000, out_bytes=350, engine="sips")
        m.set("a.CR2", "heic", output_relpath="a.heic", status="converted",
              src_bytes=1000, out_bytes=100, engine="sips")
        m.save()
        _, out = capture(rawconvert.cmd_status, self.root)
        self.assertIn("jpeg", out)
        self.assertIn("heic", out)
        self.assertIn("30%", out)   # jpeg: 600/2000
        self.assertIn("10%", out)   # heic: 100/1000
        self.assertIn("1 verified", out)


class TestCleanup(TempDirTestCase):
    def seed_tree(self):
        """a: verified jpeg + converted heic; b: converted-only jpeg."""
        for rel in ("a.CR2", "a.jpg", "a.heic", "b.CR2", "b.jpg"):
            (self.root / rel).write_bytes(b"DATA-" + rel.encode())
        m = rawconvert.Manifest(self.root)
        m.set("a.CR2", "jpeg", output_relpath="a.jpg", status="verified",
              src_bytes=10, out_bytes=3, engine="sips")
        m.set("a.CR2", "heic", output_relpath="a.heic", status="converted",
              src_bytes=10, out_bytes=2, engine="sips")
        m.set("b.CR2", "jpeg", output_relpath="b.jpg", status="converted",
              src_bytes=10, out_bytes=3, engine="sips")
        m.save()

    def trash(self, *parts):
        return self.root.joinpath(rawconvert.TRASH_DIRNAME, *parts)

    def test_cleanup_moves_verified_originals_and_rejected_outputs(self):
        self.seed_tree()
        counts, _ = capture(rawconvert.cmd_cleanup, self.root, "jpeg")
        # a.CR2 verified as jpeg -> original staged
        self.assertFalse((self.root / "a.CR2").exists())
        self.assertTrue(self.trash("originals", "a.CR2").exists())
        # a.heic is a rejected-format output -> staged
        self.assertFalse((self.root / "a.heic").exists())
        self.assertTrue(self.trash("rejected", "a.heic").exists())
        # kept-format output stays
        self.assertTrue((self.root / "a.jpg").exists())
        # b.CR2 only 'converted', not verified -> untouched
        self.assertTrue((self.root / "b.CR2").exists())
        self.assertTrue((self.root / "b.jpg").exists())
        self.assertEqual(counts["originals_staged"], 1)
        self.assertEqual(counts["rejected_staged"], 1)
        m = rawconvert.Manifest(self.root)
        m.load()
        self.assertEqual(m.get("a.CR2", "jpeg")["status"], "cleaned")
        self.assertEqual(m.get("a.CR2", "heic")["status"], "cleaned")
        self.assertEqual(m.get("b.CR2", "jpeg")["status"], "converted")

    def test_cleanup_dry_run_changes_nothing(self):
        self.seed_tree()
        counts, out = capture(rawconvert.cmd_cleanup, self.root, "jpeg",
                              dry_run=True)
        self.assertIn("a.CR2", out)
        self.assertTrue((self.root / "a.CR2").exists())
        self.assertTrue((self.root / "a.heic").exists())
        self.assertFalse(self.trash().exists())
        self.assertEqual(counts["originals_staged"], 1)

    def test_cleanup_is_idempotent(self):
        self.seed_tree()
        capture(rawconvert.cmd_cleanup, self.root, "jpeg")
        counts, _ = capture(rawconvert.cmd_cleanup, self.root, "jpeg")
        self.assertEqual(counts["originals_staged"], 0)
        self.assertEqual(counts["rejected_staged"], 0)


if __name__ == "__main__":
    unittest.main()
