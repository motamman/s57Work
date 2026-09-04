#!/usr/bin/env python3
"""
check-tile-duplicates.py — count charts stacked in a merged tileset's tiles

A merged by-band .mbtiles carries no record of which input each feature
came from, but stacked charts leave two measurable traces in a tile:

  * exact duplicates — the same S-57 object (same layer, same LNAM, same
    geometry) present more than once. That only happens when one cell was
    tiled by two inputs at the same zoom (e.g. a gap fill and the band's
    own run).
  * distinct source cells per layer — S-57 objects carry the producing
    cell's agency/FIDN/FIDS identity in LNAM, and every cell exports one
    M_COVR. Counting distinct M_COVR objects with CATCOV=1 in a tile says
    how many chart cells overlap it; when cells of different usage bands
    overlap in one tile, more than one chart is being drawn there.

Usage:
  check-tile-duplicates.py merged.mbtiles --lonlat -96.5 28.4 --zooms 9 10 11 12 13 14 15 16
  check-tile-duplicates.py merged.mbtiles --tile 13/1900/3410

Reports, per tile: total features, exact-duplicate features, the M_COVR
cell count with the usage bands present, and the DEPARE feature count
(the layer whose stacking is visible as mismatched depth shading).
"""

import argparse
import json
import math
import sqlite3
import subprocess
from collections import Counter
from pathlib import Path


def lonlat_to_tile(lon, lat, z):
    n = 1 << z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi)
            / 2.0 * n)
    return z, x, y


def has_tile(db, z, x, y):
    tms_y = (1 << z) - 1 - y
    t = "map" if db.execute("SELECT 1 FROM sqlite_master WHERE name='map'"
                            ).fetchone() else "tiles"
    return db.execute(f"SELECT 1 FROM {t} WHERE zoom_level=? AND "
                      "tile_column=? AND tile_row=?", (z, x, tms_y)).fetchone()


def decode(path, z, x, y):
    res = subprocess.run(["tippecanoe-decode", "-c", str(path),
                          str(z), str(x), str(y)],
                         capture_output=True, text=True)
    feats = []
    for line in res.stdout.splitlines():
        s = line.strip().rstrip(",")
        if s.startswith("{"):
            try:
                feats.append(json.loads(s))
            except json.JSONDecodeError:
                pass
    return feats


def analyse(feats):
    total = len(feats)
    keys = Counter()
    depare = 0
    covr_cells = set()
    bands = Counter()
    for f in feats:
        layer = f.get("tippecanoe", {}).get("layer")
        props = f.get("properties", {})
        lnam = props.get("LNAM")
        geom = json.dumps(f.get("geometry"), sort_keys=True)
        keys[(layer, lnam, geom)] += 1
        if layer == "DEPARE":
            depare += 1
        # M_COVR lands in layer "M" (pre-underscore stem) or "M_COVR".
        if layer in ("M", "M_COVR") and props.get("CATCOV") == 1 and lnam:
            covr_cells.add(lnam)
    dupes = sum(c - 1 for c in keys.values() if c > 1)
    return total, dupes, len(covr_cells), depare


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mbtiles", type=Path)
    ap.add_argument("--tile", action="append", default=[], help="z/x/y (XYZ)")
    ap.add_argument("--lonlat", nargs=2, type=float, metavar=("LON", "LAT"))
    ap.add_argument("--zooms", nargs="+", type=int,
                    default=[9, 10, 11, 12, 13, 14, 15, 16])
    args = ap.parse_args()

    targets = [tuple(int(v) for v in t.split("/")) for t in args.tile]
    if args.lonlat:
        targets += [lonlat_to_tile(args.lonlat[0], args.lonlat[1], z)
                    for z in args.zooms]

    db = sqlite3.connect(str(args.mbtiles))
    print(f"{'tile':<18}{'features':>9}{'exact dupes':>12}{'M_COVR cells':>13}{'DEPARE':>8}")
    for z, x, y in targets:
        if not has_tile(db, z, x, y):
            print(f"{f'{z}/{x}/{y}':<18}{'(no tile)':>9}")
            continue
        total, dupes, cells, depare = analyse(decode(args.mbtiles, z, x, y))
        print(f"{f'{z}/{x}/{y}':<18}{total:>9}{dupes:>12}{cells:>13}{depare:>8}")


if __name__ == "__main__":
    main()
