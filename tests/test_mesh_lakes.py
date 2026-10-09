"""The GSHHG lake mask of the mesh build (mesh.build.load_lakes,
lake_water): a lake is cut out of the OSM land only where the charts
have water areas, islands in the lake stay land, uncharted lakes stay
land. Uses the fixture in tests/fixtures/lakes/ (made-up polygons in
the GSHHG layout; the writer is at the bottom of this file)."""
import os
import unittest

try:
    import shapely
    from shapely.geometry import box
    import numpy as np
    from mesh import build as B
except ImportError:          # numpy / shapely / scipy / triangle missing
    B = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
L2 = os.path.join(ROOT, "tests", "fixtures", "lakes", "GSHHS_f_L2.shp")


@unittest.skipIf(B is None, "mesh packages not installed")
class LakeMask(unittest.TestCase):
    def setUp(self):
        B.K = 1.0                                   # no projection scale: areas in square degrees

    def test_lakes_in_box_minus_islands(self):
        la = B.load_lakes(L2, box(-71.8, 40.7, -71.3, 41.2))
        self.assertEqual(len(la), 2)
        total = sum(g.area for g in la)
        self.assertAlmostEqual(total, 0.05 * 0.07 + 0.02 * 0.02 - 0.01 * 0.01, places=9)

    def test_lakes_outside_box_are_not_loaded(self):
        la = B.load_lakes(L2, box(-71.8, 40.7, -71.7, 40.8))
        self.assertEqual(len(la), 0)

    def test_cut_is_lake_intersect_charted_water(self):
        la = B.load_lakes(L2, box(-71.8, 40.7, -71.3, 41.2))
        B.LAKES = (la, shapely.STRtree(la))
        try:
            tile = box(-71.75, 41.0, -71.5, 41.25)
            # charted water: a DEPARE covering the south half of lake 1 and water outside it
            water = [box(-71.70, 41.05, -71.50, 41.135)]
            cut = B.lake_water(tile, water)
            self.assertIsNotNone(cut)
            # lake 1 (41.10..41.17) ∩ water (41.05..41.135) = 0.05 × 0.035, minus the
            # island (41.12..41.13, inside the cut): 0.01 × 0.01
            self.assertAlmostEqual(cut.area, 0.05 * 0.035 - 0.01 * 0.01, places=9)
            # without charted water nothing is cut
            self.assertIsNone(B.lake_water(tile, []))
            # a tile holding only the uncharted lake 2: nothing is cut either
            self.assertIsNone(B.lake_water(box(-71.5, 40.7, -71.25, 40.95), [box(-71.5, 40.9, -71.3, 40.95)]))
        finally:
            B.LAKES = None

    def test_no_lakes_configured(self):
        B.LAKES = None
        self.assertIsNone(B.lake_water(box(-72, 41, -71, 42), [box(-72, 41, -71, 42)]))


# The fixture writer (run once; kept here so the shapes are on record):
#   import shapefile
#   lake1 = [(-71.60, 41.10), (-71.60, 41.17), (-71.55, 41.17), (-71.55, 41.10), (-71.60, 41.10)]
#   lake2 = [(-71.40, 40.75), (-71.40, 40.77), (-71.38, 40.77), (-71.38, 40.75), (-71.40, 40.75)]
#   isl1  = [(-71.58, 41.12), (-71.58, 41.13), (-71.57, 41.13), (-71.57, 41.12), (-71.58, 41.12)]
#   for path, rings in (("GSHHS_f_L2", [lake1, lake2]), ("GSHHS_f_L3", [isl1])):
#       w = shapefile.Writer(path, shapeType=shapefile.POLYGON); w.field("id", "N")
#       for k, r in enumerate(rings): w.poly([r]); w.record(k)
#       w.close()

if __name__ == "__main__":
    unittest.main()
