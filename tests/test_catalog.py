"""NOAA product catalog parsing and the same-band overlap decision."""
import math
import tempfile
import unittest
from pathlib import Path

from tests.helpers import pipeline

s57 = pipeline()

CATALOG = """<?xml version="1.0"?><root>
<MD_DataIdentification><title><gco:CharacterString>US2ATLPC</gco:CharacterString></title>
 <abstract><gco:CharacterString>coast guard district: 5</gco:CharacterString></abstract>
 <gml:pos>39.0 -75.0</gml:pos><gml:pos>41.5 -75.0</gml:pos>
 <gml:pos>41.5 -71.0</gml:pos><gml:pos>39.0 -71.0</gml:pos>
</MD_DataIdentification>
<MD_DataIdentification><title><gco:CharacterString>US2EC04M</gco:CharacterString></title>
 <abstract><gco:CharacterString>coast guard district: 1</gco:CharacterString></abstract>
</MD_DataIdentification>
<MD_DataIdentification><title><gco:CharacterString>US5RI1AC</gco:CharacterString></title>
 <gml:pos>41.025 -71.7</gml:pos><gml:pos>41.1 -71.625</gml:pos>
</MD_DataIdentification>
</root>"""


class ParseCatalog(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "cat.xml"
        self.path.write_text(CATALOG)
        self.cells = s57.parse_catalog(self.path)

    def test_cancelled_cells_without_polygon_drop_out(self):
        self.assertNotIn("US2EC04M", self.cells)

    def test_live_cell_fields(self):
        c = self.cells["US2ATLPC"]
        self.assertEqual((c.band, c.district), (2, "5"))
        self.assertEqual(c.bbox, (-75.0, 39.0, -71.0, 41.5))
        self.assertEqual(c.url, "https://charts.noaa.gov/ENCs/US2ATLPC.zip")

    def test_district_unknown_when_not_stated(self):
        self.assertIn("US5RI1AC", self.cells)
        self.assertEqual(self.cells["US5RI1AC"].district, "?")
        self.assertEqual(self.cells["US5RI1AC"].band, 5)


class SameBandResolution(unittest.TestCase):
    def pair(self, a, b, area):
        return s57.OverlapPair(a, b, area)

    def test_legacy_under_reschemed_is_resolved(self):
        resolved, unresolved, slivers = s57.plan_same_band_resolution(
            [self.pair("US2EC03M", "US2ATLPC", 0.5),
             self.pair("US2ATLPD", "US2EC03M", 0.2)])
        self.assertEqual(resolved, {"US2EC03M": ["US2ATLPC", "US2ATLPD"]})
        self.assertEqual(unresolved, [])
        self.assertEqual(slivers, [])

    def test_other_pairs_are_warnings(self):
        resolved, unresolved, _ = s57.plan_same_band_resolution(
            [self.pair("US2EC03M", "US2EC04M", 0.5),
             self.pair("US4NY1BY", "US4NY1BZ", 0.5)])
        self.assertEqual(resolved, {})
        self.assertEqual([r for _, r in unresolved], ["other x other", "other x other"])

    def test_edge_slivers_are_ignored(self):
        _, _, slivers = s57.plan_same_band_resolution(
            [self.pair("US2EC03M", "US2ATLPC", 2e-6)])
        self.assertEqual(len(slivers), 1)

    def test_unknown_area_is_not_a_sliver(self):
        resolved, _, slivers = s57.plan_same_band_resolution(
            [self.pair("US2EC03M", "US2ATLPC", math.nan)])
        self.assertEqual(slivers, [])
        self.assertIn("US2EC03M", resolved)


if __name__ == "__main__":
    unittest.main()
