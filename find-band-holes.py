#!/usr/bin/env python3
"""Find NOAA ENC band-coverage holes from the ENC product catalog.

For each cell of a "base" band, measures what fraction of its footprint is
covered by the union of higher-band cells. Areas covered by band 4 but not
band 5 have no z15/16 tiles in s57-to-mbtiles.py by-band output; areas
covered by band 3 but not 4/5 have nothing above z12; etc.

Usage:
  find-band-holes.py [catalog.xml] [--district 11] [--threshold 0.95]

With no catalog argument, downloads the current one from
https://charts.noaa.gov/ENCs/ENCProdCat_19115.xml into data/ (cached).
Method: sample a lat/lon grid inside each base-band cell polygon (spacing
~1/6 of the cell's short side, min 12x12) and test each point against
higher-band polygons via a 1-degree spatial bin index. Pure stdlib.
Antimeridian-crossing panels (a few band-1 cells) are skipped.
"""
import argparse
import math
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

GMD = "{http://www.isotc211.org/2005/gmd}"
GCO = "{http://www.isotc211.org/2005/gco}"
GML = "{http://www.opengis.net/gml/3.2}"


def parse_catalog(path):
    """Yield dicts: name, title, band, districts, states, polygons.

    polygons: list of [(lon, lat), ...] rings (exterior only).
    """
    cells = []
    for _, elem in ET.iterparse(path):
        if elem.tag != GMD + "MD_Metadata":
            continue
        name = title = None
        districts, states, polys = [], [], []
        ident = elem.find(f"{GMD}identificationInfo/{GMD}MD_DataIdentification")
        if ident is None:
            elem.clear()
            continue
        cit = ident.find(f"{GMD}citation/{GMD}CI_Citation")
        if cit is not None:
            t = cit.find(f"{GMD}title/{GCO}CharacterString")
            at = cit.find(f"{GMD}alternateTitle/{GCO}CharacterString")
            name = t.text.strip() if t is not None and t.text else None
            title = at.text.strip() if at is not None and at.text else ""
        for kw in ident.iter(GMD + "keyword"):
            s = kw.find(GCO + "CharacterString")
            if s is None or not s.text:
                continue
            m = re.match(r"coast guard district:\s*(\d+)", s.text)
            if m:
                districts.append(int(m.group(1)))
            m = re.match(r"state:\s*(\w+)", s.text)
            if m:
                states.append(m.group(1))
        for ring in elem.iter(GML + "LinearRing"):
            pts = []
            for pos in ring.findall(GML + "pos"):
                lat, lon = (float(x) for x in pos.text.split())
                pts.append((lon, lat))
            if len(pts) >= 4:
                polys.append(pts)
        elem.clear()
        if not name or not re.match(r"US\d", name):
            continue
        cells.append({
            "name": name, "title": title, "band": int(name[2]),
            "districts": districts, "states": states, "polygons": polys,
        })
    return cells


def poly_bbox(pts):
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return min(lons), min(lats), max(lons), max(lats)


def point_in_poly(x, y, pts):
    inside = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < xin:
                inside = not inside
    return inside


def crosses_antimeridian(pts):
    w, _, e, _ = poly_bbox(pts)
    return e - w > 180


class BinIndex:
    """1-degree spatial bins -> list of (cell, ring) for coverage tests."""

    def __init__(self):
        self.bins = defaultdict(list)

    def add(self, cell):
        for ring in cell["polygons"]:
            if crosses_antimeridian(ring):
                continue
            w, s, e, n = poly_bbox(ring)
            for bx in range(math.floor(w), math.floor(e) + 1):
                for by in range(math.floor(s), math.floor(n) + 1):
                    self.bins[(bx, by)].append((cell["name"], ring))

    def covered(self, lon, lat):
        for name, ring in self.bins.get((math.floor(lon), math.floor(lat)), ()):
            w, s, e, n = poly_bbox(ring)
            if w <= lon <= e and s <= lat <= n and point_in_poly(lon, lat, ring):
                return name
        return None


def sample_cell(cell, index):
    """Return (inside_points, covered_points, uncovered_samples)."""
    inside = covered = 0
    uncovered = []
    for ring in cell["polygons"]:
        if crosses_antimeridian(ring):
            continue
        w, s, e, n = poly_bbox(ring)
        steps = max(12, min(40, int(max(e - w, n - s) / 0.01)))
        for i in range(steps):
            for j in range(steps):
                lon = w + (e - w) * (i + 0.5) / steps
                lat = s + (n - s) * (j + 0.5) / steps
                if not point_in_poly(lon, lat, ring):
                    continue
                inside += 1
                if index.covered(lon, lat):
                    covered += 1
                else:
                    uncovered.append((lon, lat))
    return inside, covered, uncovered


def uncovered_bbox(points):
    if not points:
        return None
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return (min(lons), min(lats), max(lons), max(lats))


CATALOG_URL = "https://charts.noaa.gov/ENCs/ENCProdCat_19115.xml"


def fetch_catalog():
    import os
    import urllib.request
    path = os.path.join("data", "ENCProdCat_19115.xml")
    if not os.path.exists(path):
        os.makedirs("data", exist_ok=True)
        print(f"Downloading {CATALOG_URL} ...", file=sys.stderr)
        urllib.request.urlretrieve(CATALOG_URL, path)
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("catalog", nargs="?",
                    help="ENC product catalog XML (downloaded if omitted)")
    ap.add_argument("--base-band", type=int, default=4,
                    help="band whose footprint is checked (default 4)")
    ap.add_argument("--cover-bands", type=int, nargs="+", default=None,
                    help="bands counted as covering (default: base+1 .. 5)")
    ap.add_argument("--district", type=int, action="append",
                    help="restrict to CG district(s); repeatable")
    ap.add_argument("--threshold", type=float, default=0.95,
                    help="flag cells covered less than this fraction")
    args = ap.parse_args()

    cover_bands = args.cover_bands or list(range(args.base_band + 1, 6))
    cells = parse_catalog(args.catalog or fetch_catalog())
    print(f"Parsed {len(cells)} cells "
          f"(bands: {sorted(set(c['band'] for c in cells))})", file=sys.stderr)

    index = BinIndex()
    for c in cells:
        if c["band"] in cover_bands:
            index.add(c)

    base = [c for c in cells if c["band"] == args.base_band]
    if args.district:
        base = [c for c in base
                if any(d in args.district for d in c["districts"])]

    flagged = []
    for c in base:
        inside, covered, uncovered = sample_cell(c, index)
        if inside == 0:
            continue
        frac = covered / inside
        if frac < args.threshold:
            flagged.append((frac, c, uncovered_bbox(uncovered)))

    flagged.sort(key=lambda x: x[0])
    by_district = defaultdict(list)
    for frac, c, ub in flagged:
        d = c["districts"][0] if c["districts"] else 0
        by_district[d].append((frac, c, ub))

    cb = "+".join(str(b) for b in cover_bands)
    print(f"\nBand {args.base_band} cells covered <{args.threshold:.0%} "
          f"by band(s) {cb}: {len(flagged)} of {len(base)}\n")
    for d in sorted(by_district):
        print(f"── District {d:02d} " + "─" * 50)
        for frac, c, ub in by_district[d]:
            loc = (f"hole bbox lon [{ub[0]:.2f},{ub[2]:.2f}] "
                   f"lat [{ub[1]:.2f},{ub[3]:.2f}]") if ub else ""
            print(f"  {c['name']}  {frac:5.0%} covered  "
                  f"{','.join(c['states'])}  — {c['title']}")
            if ub:
                print(f"           {loc}")
        print()


if __name__ == "__main__":
    main()
