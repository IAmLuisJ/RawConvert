"""Tests for rawconvert.py — engines are faked; no external tools required."""
import tempfile
import unittest
from pathlib import Path

import rawconvert


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


if __name__ == "__main__":
    unittest.main()
