#!/usr/bin/env python3
"""
count-layer-by-zoom.py — features of one layer per zoom over a bounding box

For every zoom requested, every tile of the .mbtiles that intersects the
bbox is read and the features of the given layer are counted. Reports,
per zoom: tiles present in the bbox, tiles carrying the layer, feature
count, and the stored bytes of those tiles. Use it to compare two builds
of the same file (a layer that vanishes at some zooms, thinning that
removes everything, size growth per zoom).

Tiles are decoded in-process: the gzip'd Mapbox Vector Tile protobuf is
walked just far enough to read each layer's name and count its Feature
messages, so no external decoder is spawned per tile (tippecanoe-decode
takes ~6 s per tile against a 2 GB file). Standard library only.

Usage:
  count-layer-by-zoom.py 01CGD_ENCs.mbtiles --layer SOUNDG \
      --bbox -71.5 41.4 -71.2 41.75 --zooms 9 10 11 12 13 14 15 16
  count-layer-by-zoom.py before.mbtiles after.mbtiles --layer SOUNDG ...
  count-layer-by-zoom.py file.mbtiles --layer SOUNDG --layer DEPARE ...

With two or more files the table shows every file side by side; with
several --layer options one table per layer. --bbox is W S E N in
degrees. --per-tile adds the largest single-tile feature count per zoom
(the per-tile load a thinning rule is meant to bound).
"""
import argparse
import gzip
import json
import math
import sqlite3
import sys
import zlib
from pathlib import Path


def lonlat_to_tile(lon, lat, z):
    n = 1 << z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi)
            / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tiles_in_bbox(db, z, bbox):
    """(x, y, tile_data) for tiles present at zoom z inside the bbox."""
    w, s, e, n = bbox
    x0, y0 = lonlat_to_tile(w, n, z)   # top-left (XYZ)
    x1, y1 = lonlat_to_tile(e, s, z)   # bottom-right
    top = (1 << z) - 1
    rows = db.execute(
        "SELECT tile_column, tile_row, tile_data FROM tiles "
        "WHERE zoom_level=? AND tile_column BETWEEN ? AND ? "
        "AND tile_row BETWEEN ? AND ?",
        (z, x0, x1, top - y1, top - y0)).fetchall()
    return [(x, top - r, data) for x, r, data in rows]


# --- minimal MVT reader -----------------------------------------------------

def _varint(buf, i):
    result = 0
    shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _skip(buf, i, wire_type):
    if wire_type == 0:
        _, i = _varint(buf, i)
    elif wire_type == 1:
        i += 8
    elif wire_type == 2:
        length, i = _varint(buf, i)
        i += length
    elif wire_type == 5:
        i += 4
    else:
        raise ValueError(f"unsupported wire type {wire_type}")
    return i


def _decompress(data):
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    if data[:1] == b"\x78":
        return zlib.decompress(data)
    return data


def layer_feature_counts(tile_data):
    """{layer name: feature count} for one vector tile blob."""
    buf = _decompress(tile_data)
    counts = {}
    i, end = 0, len(buf)
    while i < end:
        key, i = _varint(buf, i)
        field, wt = key >> 3, key & 7
        if field == 3 and wt == 2:          # Tile.layers
            length, i = _varint(buf, i)
            layer = buf[i:i + length]
            i += length
            name, nfeat = None, 0
            j, lend = 0, len(layer)
            while j < lend:
                k, j = _varint(layer, j)
                f, w = k >> 3, k & 7
                if f == 1 and w == 2:       # Layer.name
                    ln, j = _varint(layer, j)
                    name = layer[j:j + ln].decode("utf-8", "replace")
                    j += ln
                elif f == 2 and w == 2:     # Layer.features
                    nfeat += 1
                    j = _skip(layer, j, w)
                else:
                    j = _skip(layer, j, w)
            if name is not None:
                counts[name] = counts.get(name, 0) + nfeat
        else:
            i = _skip(buf, i, wt)
    return counts


# ----------------------------------------------------------------------------

def measure(path, layers, bbox, zooms):
    db = sqlite3.connect(str(path))
    result = {layer: {} for layer in layers}
    for z in zooms:
        present = tiles_in_bbox(db, z, bbox)
        per_tile = {layer: [] for layer in layers}
        nbytes = 0
        for _, _, data in present:
            nbytes += len(data)
            counts = layer_feature_counts(data)
            for layer in layers:
                per_tile[layer].append(counts.get(layer, 0))
        for layer in layers:
            c = per_tile[layer]
            result[layer][z] = {
                "tiles": len(present),
                "tiles_with_layer": sum(1 for n in c if n),
                "features": sum(c),
                "max_per_tile": max(c) if c else 0,
                "bytes": nbytes,
            }
    return result


def fmt_size(nbytes):
    if nbytes >= 1048576:
        return f"{nbytes / 1048576:.1f}MB"
    return f"{nbytes / 1024:.0f}KB"


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mbtiles", nargs="+", type=Path)
    ap.add_argument("--layer", action="append", required=True,
                    help="layer to count (repeatable)")
    ap.add_argument("--bbox", nargs=4, type=float, required=True,
                    metavar=("W", "S", "E", "N"))
    ap.add_argument("--zooms", nargs="+", type=int,
                    default=list(range(9, 17)))
    ap.add_argument("--per-tile", action="store_true",
                    help="also show the largest single-tile count per zoom")
    ap.add_argument("--json", action="store_true",
                    help="print the raw numbers as JSON")
    args = ap.parse_args()

    results = {}
    for p in args.mbtiles:
        if not p.exists():
            sys.exit(f"missing: {p}")
        results[str(p)] = measure(p, args.layer, args.bbox, args.zooms)

    if args.json:
        print(json.dumps(results, indent=1))
        return

    names = [str(p) for p in args.mbtiles]
    width = 34 if args.per_tile else 28
    for layer in args.layer:
        print(f"layer {layer}, bbox W{args.bbox[0]} S{args.bbox[1]} "
              f"E{args.bbox[2]} N{args.bbox[3]}")
        print(f"{'zoom':>4}", end="")
        for n in names:
            print(f"  {Path(n).name[:width]:>{width}}", end="")
        print()
        legend = "features (tiles w/ layer / tiles) bytes"
        if args.per_tile:
            legend = "features [max/tile] (w/ layer / tiles) bytes"
        print(f"{'':>4}", end="")
        for _ in names:
            print(f"  {legend[-width:]:>{width}}", end="")
        print()
        for z in args.zooms:
            print(f"{z:>4}", end="")
            for n in names:
                r = results[n][layer][z]
                maxp = f" [{r['max_per_tile']:,}]" if args.per_tile else ""
                cell = (f"{r['features']:,}{maxp} ({r['tiles_with_layer']}/"
                        f"{r['tiles']}) {fmt_size(r['bytes'])}")
                print(f"  {cell:>{width}}", end="")
            print()
        print()


if __name__ == "__main__":
    main()
