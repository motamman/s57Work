"""Resume logic: what counts as fresh. Every stage is skipped on a resumed
run when its output is fresh, so a wrong answer here ships stale tiles
(a killed export resumed as complete in Sept 2026) or rebuilds
everything for nothing."""
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from tests.helpers import pipeline

s57 = pipeline()


class OutputIsFresh(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.inp = self.tmp / "in.geojson"
        self.out = self.tmp / "out.geojson"
        self.inp.write_text("x" * 200)
        self.out.write_text("y" * 200)
        now = time.time()
        os.utime(self.inp, (now - 100, now - 100))
        os.utime(self.out, (now, now))

    def test_newer_nonempty_output_is_fresh(self):
        self.assertTrue(s57.output_is_fresh(self.out, [self.inp]))

    def test_older_output_is_stale(self):
        os.utime(self.inp, None)
        time.sleep(0.01)
        os.utime(self.inp, (time.time() + 5, time.time() + 5))
        self.assertFalse(s57.output_is_fresh(self.out, [self.inp]))

    def test_tiny_or_missing_output_is_stale(self):
        self.out.write_text("{}")
        self.assertFalse(s57.output_is_fresh(self.out, [self.inp]))
        self.out.unlink()
        self.assertFalse(s57.output_is_fresh(self.out, [self.inp]))

    def test_missing_input_is_ignored(self):
        self.assertTrue(s57.output_is_fresh(self.out, [self.tmp / "gone"]))


class StampMarker(unittest.TestCase):
    def test_config_change_forces_stale_once(self):
        d = Path(tempfile.mkdtemp())
        rules = {"SOUNDG": s57.ZoomRule(13, 2.5)}
        self.assertTrue(s57._stamp_marker_stale(d, rules))   # first run
        self.assertFalse(s57._stamp_marker_stale(d, rules))  # unchanged
        self.assertTrue(s57._stamp_marker_stale(d, {"SOUNDG": s57.ZoomRule(14)}))
        self.assertTrue(s57._stamp_marker_stale(d, rules))   # changed back
        self.assertFalse(s57._stamp_marker_stale(d, rules))

    def test_old_offset_format_marker_is_stale(self):
        d = Path(tempfile.mkdtemp())
        (d / ".layer-minzoom.json").write_text('{"SOUNDG": 14}')
        self.assertTrue(s57._stamp_marker_stale(d, {"SOUNDG": s57.ZoomRule(13, 2.5)}))


class CellMarker(unittest.TestCase):
    def setUp(self):
        self.gj = Path(tempfile.mkdtemp())
        self.enc = Path("/enc/US5RI1AC.000")
        (self.gj / "DEPARE_US5RI1AC.geojson").write_text("{}" * 60)

    def test_marker_with_matching_version(self):
        s57._cell_marker(self.gj, "US5RI1AC").write_text("3.0\n")
        self.assertTrue(s57.cell_outputs_fresh(self.enc, self.gj, True, "3.0"))

    def test_version_change_is_stale(self):
        s57._cell_marker(self.gj, "US5RI1AC").write_text("3.0")
        self.assertFalse(s57.cell_outputs_fresh(self.enc, self.gj, True, "3.1"))

    def test_missing_marker_or_outputs_is_stale(self):
        self.assertFalse(s57.cell_outputs_fresh(self.enc, self.gj, True, "3.0"))
        s57._cell_marker(self.gj, "US5RI1AC").write_text("3.0")
        (self.gj / "DEPARE_US5RI1AC.geojson").unlink()
        self.assertFalse(s57.cell_outputs_fresh(self.enc, self.gj, True, "3.0"))

    def test_unknown_version_never_fresh(self):
        s57._cell_marker(self.gj, "US5RI1AC").write_text("unknown")
        self.assertFalse(s57.cell_outputs_fresh(self.enc, self.gj, True, "unknown"))


class MbtilesMetadata(unittest.TestCase):
    def test_patch_metadata_and_read_back(self):
        p = Path(tempfile.mkdtemp()) / "t.mbtiles"
        s57._patch_metadata(p, "t", drop_rate=1)
        db = sqlite3.connect(str(p))
        db.execute("INSERT INTO metadata VALUES ('minzoom', '13')")
        db.execute("INSERT INTO metadata VALUES ('maxzoom', '16')")
        db.commit()
        db.close()
        self.assertEqual(s57._mbtiles_zoom_range(p), (13, 16))
        self.assertEqual(s57._mbtiles_drop_rate(p), 1.0)
        meta = dict(sqlite3.connect(str(p)).execute(
            "SELECT name, value FROM metadata").fetchall())
        self.assertEqual(meta["type"], "S-57")
        self.assertEqual(meta["name"], "t")

    def test_file_without_drop_rate_is_never_fresh(self):
        p = Path(tempfile.mkdtemp()) / "t.mbtiles"
        s57._patch_metadata(p, "t")
        self.assertIsNone(s57._mbtiles_drop_rate(p))
        self.assertIsNone(s57._mbtiles_zoom_range(p))
        self.assertIsNone(s57._mbtiles_drop_rate(p.with_name("missing.mbtiles")))


if __name__ == "__main__":
    unittest.main()
