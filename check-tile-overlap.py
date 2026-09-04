#!/usr/bin/env python3
"""
check-tile-overlap.py — inspect how by-band tilesets overlap before tile-join

tile-join merges same-layer features from every input that has a given
tile, so any tile address present in two inputs at the same zoom ends up
carrying both charts. After the finer-wins erase, two tilesets should
share a tile address only along a chart boundary (where the coarse
chart's clipped remainder and the fine chart meet in one tile), never
across the interior of the finer chart's coverage.

Usage:
  check-tile-overlap.py data/tiles/*.mbtiles
  check-tile-overlap.py data/tiles/*.mbtiles --tile 12/1233/1533
  check-tile-overlap.py data/tiles/*.mbtiles --lonlat -71.58 41.17 --zooms 10 11 12 13

--tile takes XYZ (web) coordinates. --lonlat resolves a point to the tile
containing it at each requested zoom. For every decoded tile the script
lists, per input, the layers present and their feature counts, using
tippecanoe-decode.
"""

import argparse
import json
import math
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path


def tile_table(db):
    has_map = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                         "AND name='map'").fetchone()
    return "map" if has_map else "tiles"


def tile_addresses(path):
    db = sqlite3.connect(str(path))
    t = tile_table(db)
    rows = db.execute(f"SELECT zoom_level, tile_column, tile_row FROM {t}")
    out = defaultdict(set)
    for z, x, y in rows:
        out[z].add((x, y))
    db.close()
    return out


def lonlat_to_tile(lon, lat, z):
    n = 1 << z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi)
            / 2.0 * n)
    return z, x, y


def decode(path, z, x, y):
    """{layer: feature_count} for an XYZ tile, or None if absent."""
    res = subprocess.run(["tippecanoe-decode", "-c", str(path),
                          str(z), str(x), str(y)],
                         capture_output=True, text=True)
    if res.returncode != 0 or not res.stdout.strip():
        return None
    # With -c every feature is one line carrying its layer in the
    # "tippecanoe" member: {"type":"Feature","tippecanoe":{"layer":...}}
    counts = Counter()
    for line in res.stdout.splitlines():
        s = line.strip().rstrip(",")
        if not s.startswith("{"):
            continue
        try:
            layer = json.loads(s).get("tippecanoe", {}).get("layer")
        except json.JSONDecodeError:
            continue
        if layer:
            counts[layer] += 1
    return counts if counts else None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mbtiles", nargs="+", type=Path)
    ap.add_argument("--tile", action="append", default=[],
                    help="z/x/y (XYZ) to decode across all inputs")
    ap.add_argument("--lonlat", nargs=2, type=float, metavar=("LON", "LAT"))
    ap.add_argument("--zooms", nargs="+", type=int, default=[10, 11, 12, 13, 14, 15])
    args = ap.parse_args()

    files = [p for p in args.mbtiles if p.exists()]
    addr = {p: tile_addresses(p) for p in files}

    print("Tile counts per zoom:")
    zooms = sorted({z for a in addr.values() for z in a})
    print(f"  {'tileset':<58}" + "".join(f"{z:>8}" for z in zooms))
    for p in files:
        print(f"  {p.name:<58}"
              + "".join(f"{len(addr[p].get(z, ())):>8}" for z in zooms))

    print("\nShared tile addresses (same z/x/y in two tilesets):")
    any_shared = False
    for i, a in enumerate(files):
        for b in files[i + 1:]:
            for z in zooms:
                shared = addr[a].get(z, set()) & addr[b].get(z, set())
                if shared:
                    any_shared = True
                    total = min(len(addr[a][z]), len(addr[b][z]))
                    print(f"  z{z:<3} {a.name}  x  {b.name}: "
                          f"{len(shared)} shared ({100 * len(shared) / total:.0f}% "
                          f"of the smaller set)")
    if not any_shared:
        print("  none")

    targets = []
    for t in args.tile:
        z, x, y = (int(v) for v in t.split("/"))
        targets.append((z, x, y))
    if args.lonlat:
        for z in args.zooms:
            targets.append(lonlat_to_tile(args.lonlat[0], args.lonlat[1], z))

    for z, x, y in targets:
        print(f"\nTile {z}/{x}/{y}:")
        tms_y = (1 << z) - 1 - y
        for p in files:
            # tippecanoe-decode overzooms from a parent tile when the
            # address is absent; only decode tiles the file really has.
            if (x, tms_y) not in addr[p].get(z, set()):
                continue
            counts = decode(p, z, x, y)
            if counts is None:
                continue
            layers = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            print(f"  {p.name}: {sum(counts.values())} features  [{layers}]")


if __name__ == "__main__":
    main()
