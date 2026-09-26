"""The bundle's `scale` metadata: parsed from each cell's DSID/DSPM record
and reduced to the most detailed compilation scale in the inventory.
Signal K chart plugins read this row and default to 250000 without it,
which made every district bundle sort equal in Freeboard's chart stack."""
import unittest
from pathlib import Path

from tests.helpers import pipeline

s57 = pipeline()

OGRINFO = """Layer name: DSID
OGRFeature(DSID):0
  DSID_EXPP (Integer) = 1
  DSID_EDTN (String) = 3
  DSID_UPDN (String) = 0
  DSID_ISDT (String) = 20260108
  DSPM_HDAT (Integer) = 2
  DSPM_CSCL (Integer) = 22000
  DSPM_DUNI (Integer) = 1
"""


class ParseDsid(unittest.TestCase):
    def test_reads_edition_update_date_and_scale(self):
        self.assertEqual(s57.parse_dsid(OGRINFO),
                         {"EDTN": "3", "UPDN": "0", "ISDT": "20260108", "CSCL": "22000"})

    def test_absent_fields_are_none(self):
        self.assertEqual(s57.parse_dsid("Layer name: DSID\n"),
                         {"EDTN": None, "UPDN": None, "ISDT": None, "CSCL": None})

    def test_cancelled_cell_reads_edition_zero(self):
        text = OGRINFO.replace("DSID_EDTN (String) = 3", "DSID_EDTN (String) = 0")
        self.assertEqual(s57.parse_dsid(text)["EDTN"], "0")
        self.assertEqual(s57.cell_version(s57.parse_dsid(text)), "0.0")


class BundleScale(unittest.TestCase):
    def setUp(self):
        self.orig = s57.read_cell_versions
        self.cells = [Path("/e/US4NY1BY.000"), Path("/e/US5RI1AC.000"),
                      Path("/e/US5RI1AD.000")]

    def tearDown(self):
        s57.read_cell_versions = self.orig

    def fake(self, by_cell):
        def read(enc_files, data_dir, gdal, max_workers, why=""):
            return {e: by_cell.get(e.stem, {}) for e in enc_files}
        s57.read_cell_versions = read

    def test_most_detailed_scale_wins(self):
        self.fake({"US4NY1BY": {"CSCL": "45000"}, "US5RI1AC": {"CSCL": "22000"},
                   "US5RI1AD": {"CSCL": "40000"}})
        self.assertEqual(s57.bundle_scale(self.cells, Path("/d"), None, 1), 22000)

    def test_unreadable_scales_are_skipped(self):
        self.fake({"US4NY1BY": {"CSCL": None}, "US5RI1AC": {"CSCL": "junk"},
                   "US5RI1AD": {"CSCL": "0"}})
        # nothing readable: nominal scale of the finest band present
        self.assertEqual(s57.bundle_scale(self.cells, Path("/d"), None, 1),
                         s57.BAND_NOMINAL_SCALE[5])
        self.fake({"US4NY1BY": {"CSCL": "45000"}, "US5RI1AC": {}, "US5RI1AD": {"CSCL": "x"}})
        self.assertEqual(s57.bundle_scale(self.cells, Path("/d"), None, 1), 45000)

    def test_empty_inventory(self):
        self.assertIsNone(s57.bundle_scale([], Path("/d"), None, 1))

    def test_nominal_scales_cover_every_band(self):
        self.assertEqual(sorted(s57.BAND_NOMINAL_SCALE), sorted(s57.BAND_ZOOM))
        vals = [s57.BAND_NOMINAL_SCALE[b] for b in sorted(s57.BAND_NOMINAL_SCALE)]
        self.assertEqual(vals, sorted(vals, reverse=True))


if __name__ == "__main__":
    unittest.main()
