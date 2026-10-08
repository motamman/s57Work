"""The mesh extent (mesh/extent.py): the rectangle around the 0.25° cells
that hold z16 tiles, split into longitude clusters at gaps of more than
4° of empty columns. Pure Python, no mesh dependencies needed."""
import unittest

from mesh import extent


OFF = extent.LATTICE_DEG / 2      # the tile grid starts half a lattice cell past -180/-90
cell = extent.cell


def shifted(*vals):
    return tuple(v + OFF for v in vals)


class Extent(unittest.TestCase):
    def test_one_rectangle_around_scattered_cells(self):
        cells = {cell(-71.6, 41.1), cell(-70.1, 42.3), cell(-66.9, 44.9)}
        cl = extent.clusters(cells)
        self.assertEqual(len(cl), 1)
        c = cl[0]
        self.assertEqual(c.box, shifted(-71.75, 41.0, -66.75, 45.0))
        self.assertEqual((c.nx, c.ny), (20, 16))
        self.assertEqual(len(c.cells), 3)
        self.assertEqual(c.slug, "w072n41")

    def test_x_scale_is_cos_of_mid_latitude(self):
        import math
        c = extent.clusters({cell(-74.7, 40.0), cell(-66.0, 45.7)})[0]
        self.assertAlmostEqual(c.k, math.cos(math.radians((c.box[1] + c.box[3]) / 2)))

    def test_florida_and_puerto_rico_split(self):
        cells = {cell(-80.1, 25.7), cell(-81.5, 30.0), cell(-66.1, 18.4), cell(-65.3, 18.5)}
        cl = extent.clusters(cells)
        self.assertEqual(len(cl), 2)
        self.assertEqual([len(c.cells) for c in cl], [2, 2])
        self.assertEqual([c.box[0] for c in cl], [-81.75 + OFF, -66.25 + OFF])   # equal size: west first

    def test_small_gap_stays_one_cluster(self):
        cells = {cell(-80.0, 30.0), cell(-76.5, 34.0)}    # 3.5° apart: fewer than 16 empty columns
        self.assertEqual(len(extent.clusters(cells)), 1)

    def test_gap_just_over_threshold_splits(self):
        cells = {cell(-80.1, 30.0), cell(-75.6, 34.0)}    # cols differ by 18: 17 empty columns > 16
        self.assertEqual(len(extent.clusters(cells)), 2)

    def test_hawaii_guam_samoa_three_clusters(self):
        cells = {cell(-157.9, 21.3), cell(-156.5, 20.9), cell(144.65, 13.45), cell(-170.7, -14.3)}
        cl = extent.clusters(cells)
        self.assertEqual(len(cl), 3)
        self.assertEqual(sorted(c.slug for c in cl), ["e144n13", "w158n20", "w171s15"])

    def test_antimeridian_is_refused(self):
        with self.assertRaises(ValueError):
            extent.clusters({cell(179.9, 52.0), cell(-179.9, 52.0)})

    def test_no_seam_on_the_sounding_lattice(self):
        # Every tile seam differs from every lattice line by half a cell.
        for k in range(-40, 1500, 37):
            seam = extent.ORIGIN_LON + k * extent.TILE_DEG
            m = (seam + 75.5) / extent.LATTICE_DEG
            self.assertAlmostEqual(abs(m - round(m)), 0.5, places=6)
            seam = extent.ORIGIN_LAT + k * extent.TILE_DEG
            m = (seam - 38.7) / extent.LATTICE_DEG
            self.assertAlmostEqual(abs(m - round(m)), 0.5, places=6)

    def test_empty(self):
        self.assertEqual(extent.clusters(set()), [])


if __name__ == "__main__":
    unittest.main()
