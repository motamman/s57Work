"""Stage 4a: route-ready per-tile files (port of the experiment's
finalize_mesh.py; unchanged).

From the build output (mesh_*.npz, parts.npz, tile_*.npz) write, per
tile, an UNCOMPRESSED .npz holding everything a route needs, so a route
only reads: vertex_id, vertices, triangles (local), tri_id, neighbours
(global ids), rev (index of the shared edge in the neighbour),
shore_mult, depth_mult, is_navigable, depth, clear (per triangle), and
the boolean labels haz, mark, chanmark, struct, fair, dredged, opening plus hazv.
"""
import glob
import os
import time

import numpy as np

EXTRA = ("haz", "hazv", "mark", "chanmark", "struct", "fair", "dredged", "opening")


def finalize(src, out, log=print):
    """Write OUT/mesh_*.npz from the build directory SRC. Returns a dict
    with counts and the reverse-edge check."""
    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    files = sorted(glob.glob(f"{src}/mesh_*.npz"))
    Z = {f: dict(np.load(f)) for f in files}
    nT = sum(len(z["tri_id"]) for z in Z.values())
    NB = np.full((nT, 3), -1, dtype=np.int64)
    for z in Z.values():
        NB[z["tri_id"]] = z["neighbours"]
    t_nb = time.time() - t0
    # rev[t, k] = index of t in NB[NB[t, k]]
    t1 = time.time()
    REV = np.full((nT, 3), -1, dtype=np.int8)
    for c0 in range(0, nT, 2_000_000):
        blk = NB[c0:c0 + 2_000_000]
        tt, kk = np.nonzero(blk >= 0)
        nn = blk[tt, kk]
        REV[c0 + tt, kk] = np.argmax(NB[nn] == (tt + c0)[:, None], axis=1)
    t_rev = time.time() - t1
    # labels per triangle
    t2 = time.time()
    P = np.load(f"{src}/parts.npz"); ptile, ploc = P["part_tile"], P["part_local"]
    lab = {}

    def label_rows(pg):
        up, inv = np.unique(pg, return_inverse=True)
        d = np.empty(len(up), np.float32); c = np.empty(len(up), np.float32)
        ex = {k: np.empty(len(up), np.float32) for k in EXTRA}
        for n, p in enumerate(up):
            t = tuple(ptile[p])
            if t not in lab:
                zz = np.load(f"{src}/tile_{t[0]:03d}_{t[1]:03d}.npz")
                cols = list(zz["key_cols"])
                lab[t] = (zz["keys"], zz["part_key"], cols.index("depth"), cols.index("clear"),
                          {k: cols.index(k) for k in EXTRA})
            keys, pk, cd, cc, ci = lab[t]
            row = keys[pk[ploc[p]]]
            d[n], c[n] = row[cd], row[cc]
            for k in EXTRA:
                ex[k][n] = row[ci[k]]
        inv = inv.ravel()
        return d[inv], c[inv], {k: v[inv] for k, v in ex.items()}

    nbytes = 0
    for f, z in Z.items():
        dep, clr, ex = label_rows(z["part_global"])
        o = f"{out}/{os.path.basename(f)}"
        np.savez(o, vertex_id=z["vertex_id"], vertices=z["vertices"], triangles=z["triangles"],
                 tri_id=z["tri_id"], neighbours=z["neighbours"], rev=REV[z["tri_id"]],
                 shore_mult=z["shore_mult"], depth_mult=z["depth_mult"], is_navigable=z["is_navigable"],
                 depth=dep, clear=clr,
                 haz=ex["haz"] > 0, hazv=ex["hazv"], mark=ex["mark"] > 0, chanmark=ex["chanmark"] > 0,
                 struct=ex["struct"] > 0, fair=ex["fair"] > 0, dredged=ex["dredged"] > 0,
                 opening=ex["opening"] > 0)
        nbytes += os.path.getsize(o)
    t_write = time.time() - t2
    log(f"finalize: {len(files)} files, {nT:,} triangles; neighbours {t_nb:.1f} s, reverse edges {t_rev:.1f} s, "
        f"labels + write {t_write:.1f} s; total {time.time()-t0:.1f} s; {nbytes/1e6:.0f} MB uncompressed")
    # check: every link is mutual through rev
    ok = 0; bad = 0
    for c0 in range(0, nT, 2_000_000):
        blk = NB[c0:c0 + 2_000_000]; rv = REV[c0:c0 + 2_000_000]
        tt, kk = np.nonzero(blk >= 0)
        back = NB[blk[tt, kk], rv[tt, kk]]
        bad += int((back != tt + c0).sum()); ok += len(tt)
    log(f"check: neighbour links {ok:,}, links whose reverse edge does not point back {bad}")
    return {"files": len(files), "triangles": int(nT), "bytes": nbytes,
            "links": ok, "links_bad_reverse": bad, "seconds": time.time() - t0}
