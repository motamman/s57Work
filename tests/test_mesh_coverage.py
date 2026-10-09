"""The land rule of the mesh build: OSM land applies only outside the
charts' coverage (mesh.build.outside_coverage)."""
import unittest

try:
    import numpy as np
    import shapely
    from shapely.geometry import box
    from mesh.build import outside_coverage
except ImportError:          # numpy / shapely / scipy / triangle missing
    outside_coverage = None


@unittest.skipIf(outside_coverage is None, "mesh packages not installed")
class OutsideCoverage(unittest.TestCase):
    def test_no_coverage_keeps_every_piece(self):
        land = np.array([box(0, 0, 1, 1), box(2, 0, 3, 1)], dtype=object)
        out = outside_coverage(land, None)
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(sum(g.area for g in out), 2.0)

    def test_coverage_is_cut_out(self):
        # a 2 x 1 OSM land square; the chart covers its east half
        land = np.array([box(0, 0, 2, 1)], dtype=object)
        out = outside_coverage(land, box(1, 0, 3, 1))
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0].area, 1.0)
        self.assertEqual(out[0].bounds, (0.0, 0.0, 1.0, 1.0))

    def test_piece_fully_inside_coverage_disappears(self):
        land = np.array([box(0, 0, 1, 1), box(5, 5, 6, 6)], dtype=object)
        out = outside_coverage(land, box(-1, -1, 2, 2))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].bounds, (5.0, 5.0, 6.0, 6.0))

    def test_touching_coverage_leaves_only_polygons(self):
        # coverage sharing an edge with the land: the difference must not
        # return the shared edge as a line
        land = np.array([box(0, 0, 1, 1)], dtype=object)
        out = outside_coverage(land, box(1, 0, 2, 1))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].geom_type, "Polygon")
        self.assertAlmostEqual(out[0].area, 1.0)

    def test_empty_input(self):
        out = outside_coverage(np.empty(0, dtype=object), box(0, 0, 1, 1))
        self.assertEqual(len(out), 0)


if __name__ == "__main__":
    unittest.main()
