"""Navigation mesh built from the z16 tiles of a finished chart file.

The mesh is a constrained Delaunay triangulation of charted water, one
0.25° tile at a time, with per-triangle depth, clearance, hazard, mark
and zone labels, written in the flat binary tile format (magic
``WRPMESH1``, index version 1) that the signalk-weather-router-plus
router reads from a folder.

Pipeline (``build-mesh.py``):

1. ``decode``   — decode every z16 tile of the .mbtiles into per-layer
                  geometry lists, the way the old grid build read them
2. ``extent``   — the 0.25° cells that hold any z16 tile, grouped into
                  longitude clusters; each cluster gets its own
                  projection scale (cos of its mid-latitude)
3. ``build``    — per tile: band merge, boundaries, noding, classify and
                  dissolve, CDT, PSLG; then seam weld, one global quality
                  refinement (Triangle q20), neighbours, split into tile
                  files, checks
4. ``finalize`` — reverse edges, labels per triangle, contiguous ids,
                  binary tiles + index.json

Nothing here is district-specific: the extent, the projection and the
sounding-grid origin all follow from the input file.
"""

MESH_FORMAT_MAGIC = "WRPMESH1"
MESH_INDEX_VERSION = 1
