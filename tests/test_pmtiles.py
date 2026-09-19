"""--pmtiles: the dependency check and write_pmtiles.

The archive test builds a two-point tileset with tippecanoe, stamps it
like the pipeline does, converts it, and checks that the PMTiles decodes
to the same tiles and carries the same metadata, `type=S-57` included.
It skips without tippecanoe or go-pmtiles; the dependency-check tests
run everywhere.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers as h

POINTS = (
    '{"type":"Feature","properties":{"DEPTH":3.5,"LNAM":"A"},'
    '"geometry":{"type":"Point","coordinates":[-71.5,41.0]}}\n'
    '{"type":"Feature","properties":{"DEPTH":12.0,"LNAM":"B"},'
    '"geometry":{"type":"Point","coordinates":[-71.45,41.02]}}\n'
)


class CheckDeps(unittest.TestCase):
    def setUp(self):
        self.pipe = h.pipeline()

    def which(self, missing):
        return lambda name: None if name in missing else f"/usr/bin/{name}"

    def test_pmtiles_only_required_when_asked(self):
        with mock.patch.object(self.pipe.shutil, "which", self.which({"pmtiles"})):
            native, _ = self.pipe.check_deps(need_gdal=True, need_pmtiles=False)
        self.assertTrue(native)

    def test_missing_pmtiles_is_an_error_with_flag(self):
        with mock.patch.object(self.pipe.shutil, "which", self.which({"pmtiles"})), \
                self.assertRaises(SystemExit) as cm:
            self.pipe.check_deps(need_gdal=True, need_pmtiles=True)
        self.assertEqual(cm.exception.code, 1)


@unittest.skipUnless(h.HAVE_TIPPECANOE and h.HAVE_PMTILES,
                     "needs tippecanoe and go-pmtiles")
class WritePmtiles(unittest.TestCase):
    def setUp(self):
        self.pipe = h.pipeline()
        self.work = Path(tempfile.mkdtemp(prefix="s57pm-"))
        src = self.work / "SOUNDG.geojson"
        src.write_text(POINTS)
        self.mb = self.work / "two-points.mbtiles"
        h.subprocess.run(["tippecanoe", "-f", "-o", str(self.mb), "-Z", "10",
                          "-z", "12", "-l", "SOUNDG", "--drop-rate", "1",
                          str(src)], check=True, capture_output=True)
        self.pipe._patch_metadata(self.mb, "two-points")

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def test_archive_mirrors_mbtiles(self):
        pm = self.pipe.write_pmtiles(self.mb)
        self.assertEqual(pm, self.mb.with_suffix(".pmtiles"))
        with open(pm, "rb") as f:
            self.assertEqual(f.read(7), b"PMTiles")
        self.assertFalse(pm.with_name(pm.name + ".tmp").exists())
        a, b = h.decode_all(self.mb), h.decode_all(pm)
        self.assertEqual(a["tiles"], b["tiles"])
        self.assertGreaterEqual(len(a["tiles"]), 3)  # at least one per zoom
        self.assertEqual(b["metadata"].get("type"), "S-57")
        self.assertEqual(b["metadata"].get("name"), "two-points")
        self.assertEqual(
            [l["id"] for l in json.loads(b["metadata"]["json"])["vector_layers"]],
            ["SOUNDG"])

    def test_failed_conversion_exits_and_leaves_no_partial(self):
        bad = self.work / "not-a-tileset.mbtiles"
        bad.write_bytes(b"garbage")
        with self.assertRaises(SystemExit) as cm:
            self.pipe.write_pmtiles(bad)
        self.assertEqual(cm.exception.code, 1)
        self.assertFalse(bad.with_suffix(".pmtiles").exists())
        self.assertFalse((self.work / "not-a-tileset.pmtiles.tmp").exists())


if __name__ == "__main__":
    unittest.main()
