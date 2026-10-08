"""Stage 4b: convert the finalized mesh (per-tile .npz) into the flat
binary tile files the plugin's router reads (port of the experiment's
mesh_to_bin.py; the District 1 box became a parameter).

Changes against the .npz tiles:
  - triangle ids are renumbered so each tile's triangles form one contiguous
    block (index.json gives each tile's first id); neighbours use the new ids,
    so joining tiles at route time needs no global lookup table;
  - each triangle carries its three corners as the .npz's own float64
    coordinates (x = lon * K, y = lat; K in index.json as xScale), so the
    router needs no shared vertex table and a vertex on a tile seam is
    bit-identical in both tiles (the funnel tests points for equality);
  - shore_mult * depth_mult is stored as one multiplier;
  - the boolean labels are one flags byte.

Per tile (little-endian): 32-byte header (magic 'WRPMESH1', uint32 n,
float64 originLon, float64 originLat, 4 bytes pad), then
  corners   float64[n*6]  (x0, y0, x1, y1, x2, y2), x = lon * xScale, y = lat
  neighbours int32[n*3]   new global ids, -1 = none
  mult      float32[n]
  depth     float32[n]    -999 = unknown
  clear     float32[n]    -999 = none
  hazv      float32[n]    1e9 = no VALSOU
  rev       int8[n*3]
  flags     uint8[n]      bit0 is_navigable, 1 haz, 2 mark, 3 chanmark,
                          4 struct, 5 fair, 6 dredged
"""
import glob
import json
import math
import os
import struct
import time

import numpy as np

from . import MESH_FORMAT_MAGIC, MESH_INDEX_VERSION

MAGIC = MESH_FORMAT_MAGIC.encode()


def write_binary(src, out, box_, tile_deg, source, log=print):
    """Write OUT/mesh_*.bin and OUT/index.json from the finalized tiles in
    SRC. `box_` is (west, south, east, north); `source` is recorded in
    index.json (the chart files the mesh came from). Returns the index."""
    west, south, east, north = box_
    K = math.cos(math.radians((south + north) / 2))
    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    files = sorted(glob.glob(f"{src}/mesh_*.npz"))

    # pass 1: new ids, contiguous per tile in file order
    tiles = []
    first = 0
    for f in files:
        tid = np.load(f)["tri_id"]
        tiles.append((f, tid, first))
        first += len(tid)
    n_total = first
    new_id = np.full(n_total, -1, dtype=np.int32)
    for _, tid, f0 in tiles:
        new_id[tid] = np.arange(f0, f0 + len(tid), dtype=np.int32)
    assert (new_id >= 0).all()
    log(f"pass 1: {len(files)} tiles, {n_total:,} triangles, {time.time()-t0:.1f} s")

    # pass 2: write
    index = []
    nbytes = 0
    for f, tid, f0 in tiles:
        z = np.load(f)
        name = os.path.basename(f)[:-4]
        i, j = int(name[5:8]), int(name[9:12])
        n = len(tid)
        V = z["vertices"].astype(np.float64)
        T = z["triangles"]
        corners = V[T]                                     # (n, 3, 2) x = lon * K, lat
        ox = west + i * tile_deg
        oy = south + j * tile_deg
        c64 = corners.reshape(n * 6)
        NBg = z["neighbours"]
        NB = np.where(NBg >= 0, new_id[np.maximum(NBg, 0)], -1).astype(np.int32)
        mult = (z["shore_mult"].astype(np.float32) * z["depth_mult"].astype(np.float32)).astype(np.float32)
        flags = (
            z["is_navigable"].astype(np.uint8)
            | (z["haz"].astype(np.uint8) << 1)
            | (z["mark"].astype(np.uint8) << 2)
            | (z["chanmark"].astype(np.uint8) << 3)
            | (z["struct"].astype(np.uint8) << 4)
            | (z["fair"].astype(np.uint8) << 5)
            | (z["dredged"].astype(np.uint8) << 6)
        ).astype(np.uint8)
        o = f"{out}/{name}.bin"
        with open(o, "wb") as fh:
            fh.write(MAGIC + struct.pack("<I", n) + struct.pack("<dd", ox, oy) + b"\0" * 4)
            fh.write(c64.tobytes())
            fh.write(NB.reshape(n * 3).tobytes())
            fh.write(mult.tobytes())
            fh.write(z["depth"].astype(np.float32).tobytes())
            fh.write(z["clear"].astype(np.float32).tobytes())
            fh.write(z["hazv"].astype(np.float32).tobytes())
            fh.write(z["rev"].astype(np.int8).reshape(n * 3).tobytes())
            fh.write(flags.tobytes())
        nbytes += os.path.getsize(o)
        lo = corners.reshape(-1, 2).min(axis=0); hi = corners.reshape(-1, 2).max(axis=0)
        lo[0] /= K; hi[0] /= K
        index.append({"i": i, "j": j, "file": f"{name}.bin", "first": int(f0), "n": int(n),
                      "bbox": [float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])]})
    idx = {"version": MESH_INDEX_VERSION, "west": west, "south": south, "east": east, "north": north,
           "tileDeg": tile_deg, "xScale": K, "triangles": int(n_total), "source": source, "tiles": index}
    with open(f"{out}/index.json", "w") as fh:
        json.dump(idx, fh)
    log(f"pass 2: wrote {len(index)} tiles, {nbytes/1e6:.0f} MB; total {time.time()-t0:.1f} s")
    idx["bytes"] = nbytes
    return idx
