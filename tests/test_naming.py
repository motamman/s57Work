"""Cell and layer naming rules: the pipeline keys everything on NOAA cell
names and S-57 layer names, and a wrong split here silently merges or
drops layers (the old first-underscore split folded every M_* layer into
one called "M")."""
import unittest
from pathlib import Path

from tests.helpers import pipeline

s57 = pipeline()


class EncBand(unittest.TestCase):
    def test_band_digit(self):
        self.assertEqual(s57.enc_band(Path("US5MA1SK.000")), 5)
        self.assertEqual(s57.enc_band(Path("x/US4NY1BY.000")), 4)
        self.assertEqual(s57.enc_band(Path("us2ec03m.000")), 2)

    def test_non_noaa_name(self):
        self.assertIsNone(s57.enc_band(Path("chart.000")))
        self.assertIsNone(s57.enc_band(Path("GB5X01SW.000")))

    def test_group_by_band_buckets_unknown_as_zero(self):
        files = [Path("US5A.000"), Path("US5MA1SK.000"), Path("US3EC06M.000"),
                 Path("odd.000")]
        groups = s57.group_by_band(files)
        self.assertEqual(sorted(groups), [0, 3, 5])
        self.assertEqual(groups[0], [Path("odd.000")])


class LayerNames(unittest.TestCase):
    def test_trailing_cell_name_is_stripped(self):
        self.assertEqual(s57.layer_name_from_stem("DEPARE_US5MA1SK"), "DEPARE")
        self.assertEqual(s57.layer_name_from_stem("M_COVR_US5MA1SK"), "M_COVR")
        self.assertEqual(s57.layer_name_from_stem("TS_FEB_US2EC03M"), "TS_FEB")

    def test_single_cell_export_keeps_full_name(self):
        for stem in ("DEPARE", "M_COVR", "M_QUAL", "TS_FEB", "SOUNDG"):
            self.assertEqual(s57.layer_name_from_stem(stem), stem)

    def test_underscore_layers_are_not_split_on_first_underscore(self):
        names = {s57.layer_name_from_stem(f"{l}_US5MA1SK")
                 for l in ("M_COVR", "M_QUAL", "M_NSYS")}
        self.assertEqual(names, {"M_COVR", "M_QUAL", "M_NSYS"})

    def test_cell_name_from_stem(self):
        self.assertEqual(s57.cell_name_from_stem("M_COVR_us5ma1sk"), "US5MA1SK")
        self.assertIsNone(s57.cell_name_from_stem("M_COVR"))
        self.assertIsNone(s57.cell_name_from_stem("DEPARE_NOTACELL"))


class CellClassification(unittest.TestCase):
    def test_reschemed_region_codes(self):
        self.assertEqual(s57.classify_cell("US2ATLPC"), "reschemed")
        self.assertEqual(s57.classify_cell("us1glbcd"), "reschemed")
        self.assertEqual(s57.classify_cell("US2EC03M"), "other")
        # Only bands 1-2 carry Annex A region codes
        self.assertEqual(s57.classify_cell("US4NY1BY"), "other")

    def test_is_reschemed_name(self):
        self.assertTrue(s57.is_reschemed_name("US2ATLPC"))
        self.assertFalse(s57.is_reschemed_name("US2EC03M"))
        self.assertTrue(s57.is_reschemed_name("US4NY1BY"))
        self.assertFalse(s57.is_reschemed_name("US5NJ30M"))


class CellVersion(unittest.TestCase):
    def test_edition_and_update(self):
        self.assertEqual(s57.cell_version({"EDTN": "62", "UPDN": "3"}), "62.3")
        self.assertEqual(s57.cell_version({"EDTN": "62"}), "62.0")
        self.assertEqual(s57.cell_version({"EDTN": "0", "UPDN": "1"}), "0.1")

    def test_unreadable_dsid(self):
        self.assertEqual(s57.cell_version({}), "unknown")
        self.assertEqual(s57.cell_version({"EDTN": None}), "unknown")


if __name__ == "__main__":
    unittest.main()
