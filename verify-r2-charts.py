#!/usr/bin/env python3
"""
verify-r2-charts.py — measure every published district chart for chart
stacking and coverage, one file at a time

For each district: download the .mbtiles from the R2 mirror into a scratch
directory, measure it, append a Markdown report, delete the file. Files
are 1-10 GB each, so only one is on disk at a time.

Measurements per district
  * tiles per zoom, bounds, layer count
  * coverage: z9-z12 tile addresses under the z13 (band 4) footprint that
    have no tile, and z11-z14 addresses under the z15 (band 5) footprint —
    the "gap" measure; compare with the previous release
  * stacking: at a few well-known locations, for every zoom 9-16, the
    number of distinct chart cells in the tile and the number of exact
    duplicate features (same layer, LNAM and geometry). One cell and zero
    duplicates is a clean tile; a cell boundary legitimately shows two.

Usage:
  verify-r2-charts.py                       # all districts
  verify-r2-charts.py 08CGD 13CGD           # some
  verify-r2-charts.py --keep 01CGD          # keep the downloaded file
  verify-r2-charts.py --report out.md ...

Requires tippecanoe-decode and curl. The location table below is the
place to add probe points.
"""

import argparse
import math
import os
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

R2_BASE = "https://pub-281728c6a69f4f549cf0ec4e83f9fcde.r2.dev/US-ENC/charts"

# district -> [(name, lon, lat)]
PROBES = {
    "01CGD": [("Block Island RI", -71.58, 41.17), ("Boston approach", -70.85, 42.35),
              ("Jonesport ME (fill area)", -67.8, 44.5), ("Lubec ME (fill area)", -67.0, 44.85)],
    "05CGD": [("Chesapeake entrance", -76.0, 37.0), ("Cape Hatteras", -75.6, 35.25),
              ("Delaware Bay", -75.1, 39.0), ("NY Harbor approach", -74.0, 40.5)],
    "07CGD": [("Miami", -80.1, 25.75), ("Straits of Florida", -79.8, 25.0),
              ("Charleston", -79.9, 32.75), ("San Juan PR", -66.1, 18.5)],
    "08CGD": [("Houma LA (fill area)", -90.7, 29.4), ("Corpus Christi (fill area)", -97.2, 27.8),
              ("Galveston (fill area)", -94.8, 29.3), ("Mobile Bay", -88.0, 30.5), ("Tampa Bay", -82.7, 27.7)],
    "09CGD": [("Chicago", -87.6, 41.9), ("Detroit River", -83.1, 42.2),
              ("Duluth", -92.05, 46.75), ("Buffalo", -78.9, 42.9)],
    "11CGD": [("San Diego approach", -117.3, 32.65), ("Santa Catalina Gulf", -117.7, 33.0),
              ("LA/Long Beach", -118.2, 33.7), ("Santa Barbara Channel", -119.7, 34.3)],
    "13CGD": [("Puget Sound Seattle", -122.4, 47.6), ("Strait of Juan de Fuca", -124.2, 48.35),
              ("Bellingham Bay", -122.55, 48.75), ("Columbia River mouth", -124.0, 46.25)],
    "14CGD": [("Honolulu", -157.9, 21.3), ("Kahului Maui", -156.47, 20.9), ("Guam Apra", 144.65, 13.45)],
    "17CGD": [("Juneau", -134.4, 58.3), ("Anchorage / Knik Arm", -149.9, 61.2),
              ("Dutch Harbor", -166.5, 53.9), ("Ketchikan", -131.65, 55.35)],
}
ZOOMS = list(range(9, 17))


def lonlat_to_tile(lon, lat, z):
    n = 1 << z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2.0 * n)
    return z, x, y


def download(district, dest, attempts=8):
    """Resumable download: R2 drops long transfers now and then (curl
    exit 56), so each retry continues from the bytes already on disk."""
    url = f"{R2_BASE}/{district}_ENCs.mbtiles"
    t0 = time.time()
    last = None
    for _ in range(attempts):
        res = subprocess.run(["curl", "-sSL", "-C", "-", "--retry", "3",
                              "-o", str(dest), url])
        if res.returncode == 0:
            return time.time() - t0
        last = res.returncode
        time.sleep(5)
    raise subprocess.CalledProcessError(last, ["curl", url])


def tile_sets(db, table):
    tiles = defaultdict(set)
    for z, x, y in db.execute(f"SELECT zoom_level, tile_column, tile_row FROM {table}"):
        tiles[z].add((x, y))
    return tiles


def ancestors(s, z_from, z_to):
    d = z_from - z_to
    return {(x >> d, y >> d) for x, y in s}


def coverage_gaps(tiles):
    rows = []
    if 13 in tiles:
        for z in (9, 10, 11, 12):
            want = ancestors(tiles[13], 13, z)
            rows.append((f"z{z} under band-4 footprint", len(want - tiles.get(z, set())), len(want)))
    if 15 in tiles:
        for z in (11, 12, 13, 14):
            want = ancestors(tiles[15], 15, z)
            rows.append((f"z{z} under band-5 footprint", len(want - tiles.get(z, set())), len(want)))
    return rows


def decode(path, z, x, y):
    res = subprocess.run(["tippecanoe-decode", "-c", str(path), str(z), str(x), str(y)],
                         capture_output=True, text=True)
    import json
    feats = []
    for line in res.stdout.splitlines():
        s = line.strip().rstrip(",")
        if s.startswith("{"):
            try:
                feats.append(json.loads(s))
            except json.JSONDecodeError:
                pass
    return feats


def stacking(feats):
    import json
    keys = Counter()
    cells = set()
    for f in feats:
        layer = f.get("tippecanoe", {}).get("layer")
        props = f.get("properties", {})
        lnam = props.get("LNAM")
        keys[(layer, lnam, json.dumps(f.get("geometry"), sort_keys=True))] += 1
        if layer in ("M", "M_COVR") and props.get("CATCOV") == 1 and lnam:
            cells.add(lnam)
    dupes = sum(c - 1 for c in keys.values() if c > 1)
    return len(feats), dupes, len(cells)


def measure(district, path, out):
    db = sqlite3.connect(str(path))
    table = "map" if db.execute("SELECT 1 FROM sqlite_master WHERE name='map'").fetchone() else "tiles"
    meta = dict(db.execute("SELECT name, value FROM metadata"))
    tiles = tile_sets(db, table)
    size_mb = path.stat().st_size / 1048576

    out.write(f"\n## {district}  ({size_mb:.0f} MB)\n\n")
    out.write(f"bounds: `{meta.get('bounds')}`  zooms: {meta.get('minzoom')}-{meta.get('maxzoom')}\n\n")
    out.write("Tiles per zoom: " + ", ".join(f"z{z}={len(tiles[z])}" for z in sorted(tiles)) + "\n\n")

    out.write("Coverage gaps (tile addresses with no tile):\n\n")
    out.write("| check | missing | of |\n|---|---|---|\n")
    for name, missing, total in coverage_gaps(tiles):
        out.write(f"| {name} | {missing} | {total} |\n")

    out.write("\nStacking at probe points (cells = distinct chart cells in the tile; dupes = exact duplicate features):\n\n")
    out.write("| location | " + " | ".join(f"z{z}" for z in ZOOMS) + " |\n")
    out.write("|---|" + "---|" * len(ZOOMS) + "\n")
    worst_cells = 0
    total_dupes = 0
    for name, lon, lat in PROBES.get(district, []):
        row = []
        for z in ZOOMS:
            _, x, y = lonlat_to_tile(lon, lat, z)
            tms_y = (1 << z) - 1 - y
            if (x, tms_y) not in tiles.get(z, set()):
                row.append("–")
                continue
            n, dupes, cells = stacking(decode(path, z, x, y))
            worst_cells = max(worst_cells, cells)
            total_dupes += dupes
            row.append(f"{cells}c/{dupes}d" if dupes else f"{cells}c")
        out.write(f"| {name} | " + " | ".join(row) + " |\n")
    out.write(f"\nMax cells in one probed tile: {worst_cells}. Duplicate features across all probed tiles: {total_dupes}.\n")
    out.flush()
    db.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # Alaska (17CGD) is never run by default: 10 GB, and excluded at the
    # owner's request. Name it explicitly to measure it.
    ap.add_argument("districts", nargs="*",
                    default=[d for d in PROBES if d != "17CGD"])
    ap.add_argument("--scratch", type=Path, default=Path(os.environ.get("VERIFY_SCRATCH", "/tmp")))
    ap.add_argument("--report", type=Path, default=Path("verify-report.md"))
    ap.add_argument("--keep", action="store_true", help="do not delete downloaded files")
    args = ap.parse_args()

    args.scratch.mkdir(parents=True, exist_ok=True)
    with open(args.report, "a") as out:
        out.write(f"\n# Chart verification {time.strftime('%Y-%m-%d %H:%M')}\n")
        for d in args.districts:
            path = args.scratch / f"{d}_ENCs.mbtiles"
            print(f"[{d}] downloading...", flush=True)
            try:
                secs = download(d, path)
            except subprocess.CalledProcessError as e:
                print(f"[{d}] DOWNLOAD FAILED: {e}", flush=True)
                out.write(f"\n## {d}\n\nDOWNLOAD FAILED\n")
                continue
            print(f"[{d}] downloaded in {secs:.0f}s, measuring...", flush=True)
            measure(d, path, out)
            if not args.keep:
                path.unlink()
            print(f"[{d}] DONE", flush=True)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
