"""Stage 2: the mesh extent, derived from the chart file.

The experiment meshed a hand-drawn District 1 rectangle, every 0.25°
tile in it. The agnostic equivalent: the rectangle around all cells
that hold a z16 tile. A district whose cells fall into longitude
groups separated by more than GAP_DEG of empty columns (07CGD: Florida
and Puerto Rico; 14CGD: Hawaii, Guam, American Samoa) is split into one
rectangle per group, each built as its own mesh with its own projection
scale (cos of its mid-latitude), exactly as the experiment treated D1.
"""
import math

TILE_DEG = 0.25
GAP_DEG = 4.0            # empty longitude span that separates two clusters
# The tile grid starts half a sounding-grid cell past -180/-90. The build
# draws sounding squares and the fairway reach on the old routing grid's
# 0.00025° lattice (origin -75.5, 38.7); a tile seam lying exactly on a
# lattice line puts every square abutting it on one side only, so the
# labels differ across the seam, the seam is kept as a constraint, and
# the refinement inflates (01CGD, Oct 2026: 14,954 kept seam segments
# instead of 26, 5.7x slower). With this offset a seam and a lattice line
# always differ by half a cell: (-180 + 0.000125 + k*0.25) - (-75.5 +
# m*0.00025) = 0 would need m = 418000.5 + 1000k.
LATTICE_DEG = 0.00025
ORIGIN_LON = -180.0 + LATTICE_DEG / 2
ORIGIN_LAT = -90.0 + LATTICE_DEG / 2
_COLS = int(round(360 / TILE_DEG))


def cell(lon, lat, tile_deg=TILE_DEG):
    """The (col, row) tile cell holding a point."""
    return (math.floor((lon - ORIGIN_LON) / tile_deg), math.floor((lat - ORIGIN_LAT) / tile_deg))


class Cluster:
    def __init__(self, cells, tile_deg=TILE_DEG):
        self.cells = frozenset(cells)
        cols = [c for c, _ in cells]
        rows = [r for _, r in cells]
        self.col0, self.col1 = min(cols), max(cols) + 1
        self.row0, self.row1 = min(rows), max(rows) + 1
        self.tile_deg = tile_deg
        self.box = (ORIGIN_LON + self.col0 * tile_deg, ORIGIN_LAT + self.row0 * tile_deg,
                    ORIGIN_LON + self.col1 * tile_deg, ORIGIN_LAT + self.row1 * tile_deg)
        self.nx = self.col1 - self.col0
        self.ny = self.row1 - self.row0

    @classmethod
    def from_box(cls, box_, tile_deg=TILE_DEG):
        """A cluster covering an explicit rectangle (the `--box` diagnostic
        override). The tile grid starts at the rectangle's south-west
        corner, as the experiment's did; `cells` is empty."""
        c = cls.__new__(cls)
        w, s, e, n = (float(v) for v in box_)
        c.cells = frozenset()
        c.tile_deg = tile_deg
        c.box = (w, s, e, n)
        c.nx = math.ceil((e - w) / tile_deg)
        c.ny = math.ceil((n - s) / tile_deg)
        c.col0 = c.row0 = 0
        c.col1, c.row1 = c.nx, c.ny
        return c

    @property
    def k(self):
        """x scale: cos of the rectangle's mid-latitude (the experiment's K)."""
        return math.cos(math.radians((self.box[1] + self.box[3]) / 2))

    @property
    def slug(self):
        """Stable folder name for a multi-cluster district: hemisphere +
        whole degrees of the rectangle's west and south edges."""
        w, s = self.box[0], self.box[1]
        return f"{'w' if w < 0 else 'e'}{abs(int(math.floor(w))):03d}{'s' if s < 0 else 'n'}{abs(int(math.floor(s))):02d}"

    def describe(self):
        return {"box": list(self.box), "tiles_x": self.nx, "tiles_y": self.ny,
                "tiles": self.nx * self.ny, "cells_with_z16": len(self.cells),
                "x_scale": self.k}


def clusters(cells, tile_deg=TILE_DEG, gap_deg=GAP_DEG):
    """Split cells into longitude clusters. Raises if the cells straddle
    the antimeridian (occupied columns at both ends of the range within
    one gap of ±180°), which one rectangle cannot represent."""
    if not cells:
        return []
    gap_cols = int(round(gap_deg / tile_deg))
    cols = sorted({c for c, _ in cells})
    if cols[0] < gap_cols and cols[-1] >= _COLS - gap_cols:
        raise ValueError("z16 cells on both sides of the antimeridian; one rectangle cannot hold them")
    groups, cur = [], [cols[0]]
    for a, b in zip(cols, cols[1:]):
        if b - a - 1 > gap_cols:
            groups.append(cur)
            cur = []
        cur.append(b)
    groups.append(cur)
    out = []
    for g in groups:
        gs = set(g)
        out.append(Cluster([c for c in cells if c[0] in gs], tile_deg))
    # largest first, so a single-cluster district is always cluster 0
    out.sort(key=lambda c: (-len(c.cells), c.box[0]))
    return out
