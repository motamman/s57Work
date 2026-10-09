#!/usr/bin/env python3
"""Diagnostic: how much of a chart's water the mesh's land masks turn
into land.

The mesh build (build-mesh.py) takes land from the OSM land polygons
outside the charts' coverage (M_COVR) and from the chart inside it.
Until 2026-10-09 OSM land overrode the chart everywhere, which turned
the Great Lakes, the Hudson and every harbour or river behind the OSM
coastline into land. This tool measures, per 0.25 deg cell of a chart's
z16 tiles, the charted water area (DEPARE + DRGARE + UNSARE) and how
much of it is

  * inside the OSM land polygons             (land when OSM overrides the chart)
  * inside OSM land and outside the coverage (land under the current rule: charted
                                              water the z16 tiles carry no M_COVR for)

so lost harbours, river mouths and connecting channels can be found and
sized, and the rule's residue checked after a decoder or chart change.

    check-mesh-land-mask.py --decoded data/mesh/09CGD/z16_layers --land land/land_polygons.shp \\
        [--mbtiles chart.mbtiles  # decode first, into --decoded] [--top 25] [-o report.json]

Needs the mesh packages (mesh/requirements.txt). Areas are in km^2 on a
cos(latitude) approximation.
"""
import argparse
import json
import math
import os
import pickle
import sys
import time

import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry import box, shape

WATER = ("DEPARE", "DRGARE", "UNSARE")
DEG_KM = 111.32


def load_water(decoded):
    polys = []
    for L in WATER:
        p = os.path.join(decoded, f"{L}.pkl")
        if not os.path.exists(p):
            continue
        with open(p, "rb") as fh:
            items = pickle.load(fh)
        g = shapely.from_wkb([w for w, _ in items])
        g = g[np.isin(shapely.get_type_id(g), (3, 6))]
        polys.extend(shapely.get_parts(g))
    arr = np.array(polys, dtype=object)
    return arr[~shapely.is_empty(arr)]


def load_coverage(decoded):
    p = os.path.join(decoded, "M_COVR.pkl")
    if not os.path.exists(p):
        return np.empty(0, dtype=object)
    with open(p, "rb") as fh:
        items = pickle.load(fh)
    keep = [w for w, pj in items if (json.loads(pj) if isinstance(pj, str) else pj).get("CATCOV") == 1]
    g = shapely.from_wkb(keep) if keep else np.empty(0, dtype=object)
    g = g[np.isin(shapely.get_type_id(g), (3, 6))] if len(g) else g
    return np.array([p for gg in g for p in shapely.get_parts(gg) if not p.is_empty], dtype=object)


def load_shp(path, bbox):
    import shapefile
    return [shape(s.__geo_interface__) for s in shapefile.Reader(path).iterShapes(bbox=list(bbox))]


def polygonal(geom):
    """Only the polygon parts of an overlay result (touching inputs also
    yield lines and points, which GEOS refuses in a later overlay)."""
    parts = shapely.get_parts(geom)
    parts = parts[np.isin(shapely.get_type_id(parts), (3, 6))]
    return shapely.union_all(parts) if len(parts) else shapely.Polygon()


def km2(geom, lat):
    return geom.area * DEG_KM * DEG_KM * math.cos(math.radians(lat))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--decoded", required=True, help="z16_layers directory of decoded pickles (mesh.decode)")
    ap.add_argument("--mbtiles", nargs="*", default=[], help="decode these chart files into --decoded first")
    ap.add_argument("--land", required=True, help="OSM land_polygons.shp")
    ap.add_argument("--cell", type=float, default=0.25, help="cell size, degrees (default 0.25)")
    ap.add_argument("--top", type=int, default=25, help="cells to list, worst first (default 25)")
    ap.add_argument("-j", "--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("-o", "--out", help="write the full per-cell report as JSON")
    a = ap.parse_args(argv)

    if a.mbtiles:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from mesh.decode import decode_to_pickles
        decode_to_pickles(a.mbtiles, a.decoded, a.workers)

    t0 = time.time()
    water = load_water(a.decoded)
    if not len(water):
        sys.exit("no DEPARE/DRGARE/UNSARE pieces in " + a.decoded)
    wtree = STRtree(water)
    b = shapely.total_bounds(water)
    print(f"{len(water):,} charted water pieces, bounds {np.round(b, 3).tolist()}, {time.time()-t0:.0f} s", flush=True)

    osm = load_shp(a.land, (b[0] - 0.01, b[1] - 0.01, b[2] + 0.01, b[3] + 0.01))
    osm = np.array([p for g in osm for p in shapely.get_parts(g)], dtype=object)
    otree = STRtree(osm)
    print(f"{len(osm):,} OSM land polygons", flush=True)
    cov = load_coverage(a.decoded)
    ctree = STRtree(cov) if len(cov) else None
    print(f"{len(cov):,} coverage pieces (M_COVR, CATCOV 1)" + ("" if len(cov) else "  <- none decoded: an old z16_layers?"), flush=True)

    west = math.floor(b[0] / a.cell) * a.cell
    south = math.floor(b[1] / a.cell) * a.cell
    nx = math.ceil((b[2] - west) / a.cell)
    ny = math.ceil((b[3] - south) / a.cell)
    cells = []
    tot = {"water": 0.0, "osm_land": 0.0, "osm_land_not_covered": 0.0}
    for i in range(nx):
        for j in range(ny):
            cb = box(west + i * a.cell, south + j * a.cell, west + (i + 1) * a.cell, south + (j + 1) * a.cell)
            wi = wtree.query(cb)
            if not len(wi):
                continue
            w = polygonal(shapely.union_all(shapely.intersection(water[wi], cb), grid_size=1e-7))
            if w.is_empty:
                continue
            lat = cb.centroid.y
            oi = otree.query(cb)
            lost = polygonal(shapely.intersection(w, polygonal(shapely.union_all(shapely.intersection(osm[oi], cb))),
                                                  grid_size=1e-7)) if len(oi) else shapely.Polygon()
            lost_cov = lost
            if ctree is not None and not lost.is_empty:
                ci = ctree.query(cb)
                if len(ci):
                    lost_cov = polygonal(shapely.difference(
                        lost, polygonal(shapely.union_all(shapely.intersection(cov[ci], cb))), grid_size=1e-7))
            rec = {"i": i, "j": j, "west": round(cb.bounds[0], 6), "south": round(cb.bounds[1], 6),
                   "water_km2": round(km2(w, lat), 3), "osm_land_km2": round(km2(lost, lat), 3),
                   "osm_land_not_covered_km2": round(km2(lost_cov, lat), 3)}
            ex = lost_cov if not lost_cov.is_empty else lost
            if not ex.is_empty:
                c = ex.representative_point()
                rec["example_lonlat"] = [round(c.x, 5), round(c.y, 5)]
            cells.append(rec)
            tot["water"] += rec["water_km2"]; tot["osm_land"] += rec["osm_land_km2"]
            tot["osm_land_not_covered"] += rec["osm_land_not_covered_km2"]
    key = "osm_land_not_covered_km2" if ctree is not None else "osm_land_km2"
    cells.sort(key=lambda r: -r[key])
    print(f"\ncharted water {tot['water']:,.0f} km2; inside OSM land {tot['osm_land']:,.0f} km2 "
          f"({100*tot['osm_land']/tot['water']:.1f} %)"
          + (f"; inside OSM land and outside the charts' coverage {tot['osm_land_not_covered']:,.0f} km2 "
             f"({100*tot['osm_land_not_covered']/tot['water']:.1f} %)" if ctree is not None else ""))
    print(f"\nworst {min(a.top, len(cells))} cells by {key} (cell south-west corner, example point inside the lost water):")
    for r in cells[:a.top]:
        print(f"  {r['west']:9.4f} {r['south']:8.4f}  water {r['water_km2']:8.2f}  lost {r[key]:8.2f} km2"
              + (f"  e.g. {r['example_lonlat'][0]}, {r['example_lonlat'][1]}" if "example_lonlat" in r else ""))
    if a.out:
        with open(a.out, "w") as fh:
            json.dump({"decoded": a.decoded, "land": a.land, "cell_deg": a.cell,
                       "totals_km2": tot, "cells": cells}, fh, indent=1)
        print(f"\nreport: {a.out}")
    print(f"{time.time()-t0:.0f} s")


if __name__ == "__main__":
    main()
