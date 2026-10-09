"""Stage 1: decode the z16 tiles of MBTiles chart files into per-layer
geometry lists, the way the old grid build read them (port of the
experiment's mbtiles_to_layers.py):

  * zoom 16 only, no fallback to coarser tiles
  * gzip + mapbox_vector_tile.decode(data, y_coord_down=True)
  * tile pixel -> lon/lat: lon = tw + px/4096*(te-tw), lat = tn - py/4096*(tn-ts)
  * each tile's features are clipped to that tile's own bounds

Several MBTiles sources are decoded independently. Each piece keeps its
source ("__src") and its position in the tile ("__ord"): within one
source a dredged area replaces the depth and the LAST obstruction wins,
so the order matters.

Output: one pickle per layer in OUT, a list of (wkb, properties-json)
tuples — the same files the experiment wrote to data/z16_layers/.
"""
import gzip
import json
import math
import os
import pickle
import sqlite3
import time
from multiprocessing import get_context

import mapbox_vector_tile
import shapely
from shapely.geometry import box, shape

OBS_RANK = {"OBSTRN": 0, "UWTROC": 1, "WRECKS": 2}     # obstruction layer order
ZOOM = 16
EXTENT = 4096
LAYERS = {"LNDARE", "SLCONS", "PONTON", "FLODOC", "HULKES", "CAUSWY", "DAMCON", "GATCON", "DRYDOC",
          "MORFAC", "OFSPLF", "PILPNT", "DEPARE", "DRGARE", "SOUNDG", "UNSARE", "OBSTRN", "WRECKS",
          "UWTROC", "BOYLAT", "BOYCAR", "BOYSPP", "BOYSAW", "BOYISD", "BOYINB", "BCNLAT", "BCNCAR",
          "BCNSPP", "BCNSAW", "BCNISD", "LIGHTS", "DAYMAR", "PILBOP", "BRIDGE", "CBLOHD", "PIPOHD",
          "CONVYR", "FAIRWY", "RECTRC", "NAVLNE", "TSSLPT", "TSEZNE", "RESARE", "M_QUAL", "M_COVR"}


def tile_bounds_xyz(z, x, y):
    n = 2 ** z
    w = x / n * 360.0 - 180.0
    e = (x + 1) / n * 360.0 - 180.0
    nl = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    sl = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return w, sl, e, nl


def z16_tiles(mbtiles):
    """[(tile_column, tile_row_tms)] of every z16 tile in the file."""
    con = sqlite3.connect(mbtiles)
    try:
        return con.execute("SELECT tile_column, tile_row FROM tiles WHERE zoom_level=?",
                           (ZOOM,)).fetchall()
    finally:
        con.close()


def z16_cells(mbtiles_paths, tile_deg, cell_of):
    """The set of tile cells that contain any z16 tile of the inputs;
    `cell_of(lon, lat)` maps a point to its (col, row) (mesh.extent.cell).
    A z16 tile is tiny (~0.0055 deg) and never straddles a cell edge by
    more than one cell, so all four corners are tested."""
    cells = set()
    n = 2 ** ZOOM
    for p in mbtiles_paths:
        for x, y_tms in z16_tiles(p):
            w, s, e, nl = tile_bounds_xyz(ZOOM, x, (n - 1) - y_tms)
            for lon in (w + 1e-9, e - 1e-9):
                for lat in (s + 1e-9, nl - 1e-9):
                    cells.add(cell_of(lon, lat))
    return cells


def _decode(task):
    src, tx, ty_tms, data = task
    y_xyz = (2 ** ZOOM - 1) - ty_tms
    tw, ts, te, tn = tile_bounds_xyz(ZOOM, tx, y_xyz)
    try:
        tile = mapbox_vector_tile.decode(gzip.decompress(data), y_coord_down=True)
    except Exception:
        return {"__failed__": [(src, tx, ty_tms)]}
    tb = box(tw, ts, te, tn)
    sx, sy = (te - tw) / EXTENT, (tn - ts) / EXTENT
    out = {}
    for L, layer in tile.items():
        if L not in LAYERS:
            continue
        ext = layer.get("extent", EXTENT)
        if ext != EXTENT:
            # The pixel -> degree mapping above assumes tippecanoe's default
            # 4096 extent, as the grid build did. Anything else is a build
            # change upstream, not something to paper over here.
            raise RuntimeError(f"{src} z{ZOOM}/{tx}/{y_xyz} layer {L}: MVT extent {ext}, expected {EXTENT}")
        for fi, f in enumerate(layer.get("features", [])):
            try:
                g = shape(f["geometry"])
            except Exception:
                continue
            g = shapely.transform(g, lambda c: c * [sx, -sy] + [tw, tn])
            if g.geom_type in ("Point", "MultiPoint"):
                g = shapely.intersection(g, tb)        # points: inside this tile only
            else:
                g = shapely.intersection(shapely.make_valid(g), tb)
            if g.is_empty:
                continue
            pr = dict(f.get("properties", {}))
            pr["__src"] = src
            pr["__ord"] = OBS_RANK.get(L, 0) * 1_000_000 + fi
            out.setdefault(L, []).append((shapely.to_wkb(g), json.dumps(pr)))
    return out


def _tasks(mbtiles_paths):
    for MBT in mbtiles_paths:
        src = os.path.basename(MBT)
        con = sqlite3.connect(MBT)
        cur = con.execute("SELECT tile_column, tile_row, tile_data FROM tiles WHERE zoom_level=?",
                          (ZOOM,))
        for row in cur:
            yield (src,) + tuple(row)
        con.close()


def decode_to_pickles(mbtiles_paths, out_dir, workers, log=print):
    """Decode every z16 tile of the inputs into OUT/<LAYER>.pkl.
    Returns {"tiles": n, "failed": [...], "pieces": {layer: n}}."""
    os.makedirs(out_dir, exist_ok=True)
    t0 = time.time()
    acc, n_tiles, failed = {}, 0, []
    with get_context("fork").Pool(workers) as pool:
        for res in pool.imap_unordered(_decode, _tasks(mbtiles_paths), chunksize=64):
            n_tiles += 1
            for L, v in res.items():
                if L == "__failed__":
                    failed += v
                else:
                    acc.setdefault(L, []).extend(v)
            if n_tiles % 20000 == 0:
                log(f"{n_tiles:,} tiles in {time.time()-t0:.0f} s")
    for L, v in acc.items():
        with open(os.path.join(out_dir, f"{L}.pkl"), "wb") as fh:
            pickle.dump(v, fh)
    pieces = {L: len(v) for L, v in sorted(acc.items())}
    log(f"done: {n_tiles:,} z16 tiles decoded in {time.time()-t0:.1f} s; "
        f"tiles that failed to decode: {len(failed)}")
    log("pieces per layer: " + ", ".join(f"{L} {n:,}" for L, n in pieces.items()))
    return {"tiles": n_tiles, "failed": failed, "pieces": pieces, "seconds": time.time() - t0}
