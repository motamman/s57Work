#!/usr/bin/env python3 -u
"""
s57-to-mbtiles.py — Convert S-57 ENC charts to vector MBTiles

Takes one or more ZIP files (or directories) of S-57 ENC files (.000) and
produces a single merged vector MBTiles file.

─────────────────────────────────────────────────────────────────────────────
MODES
─────────────────────────────────────────────────────────────────────────────

SINGLE SOURCE:
  %(prog)s NY_ENCs.zip
  %(prog)s NY_ENCs.zip -o ny-charts.mbtiles --minzoom 9 --maxzoom 16

TWO-SOURCE MERGE (coarse + detail):
  %(prog)s region03.zip RI_detail.zip --split 12 -o ri-merged.mbtiles

MULTI-SOURCE (explicit zoom ranges):
  %(prog)s --sources region03.zip:9-11 RI_detail.zip:12-16 -o merged.mbtiles

BY-BAND (recommended for multi-state regions):
  %(prog)s CT_ENCs.zip RI_ENCs.zip NY_ENCs.zip --by-band -o ct-ri-ny.mbtiles

SKIP GDAL (use existing GeoJSON):
  %(prog)s --geojson-dir ./data/geojson/band3/ --minzoom 11 --maxzoom 12

─────────────────────────────────────────────────────────────────────────────
PIPELINE (per band or source)
  1. Extract ZIPs → data/enc/
  2. ogr2ogr (native or container) → data/geojson/
  3. Consolidate per-layer GeoJSON → data/merged/
  4. tippecanoe (one per band, full zoom range) → data/tiles/
  5. tile-join → final .mbtiles

All artifacts stored in ./data/, nothing deleted between runs.
─────────────────────────────────────────────────────────────────────────────
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

__version__ = "0.6.0"

GDAL_IMAGE = "ghcr.io/osgeo/gdal:alpine-small-latest"
SKIP_LAYERS = {"DSID", "C_AGGR", "C_ASSO", "Generic"}
DATA_DIR = Path("data")

BAND_ZOOM: Dict[int, Tuple[int, int, str, str]] = {
    1: (7,  8,  "overview",  "~1:3,500,000"),
    2: (9,  10, "general",   "~1:700,000"),
    3: (11, 12, "coastal",   "~1:90,000"),
    4: (13, 14, "approach",  "~1:22,000"),
    5: (15, 16, "harbour",   "~1:8,000"),
    6: (17, 18, "berthing",  "~1:3,000"),
}

# Per-layer minzoom offset (in zoom levels) from a band's minzoom.
# Layers not listed default to 0 (emit from band's bottom zoom).
# Heavy/dense layers get +1 so they only appear at the top of the band.
LAYER_MIN_ZOOM_OFFSET: Dict[str, int] = {
    # Aids to navigation — only meaningful at approach detail or finer
    "LIGHTS": 1,
    "BCNLAT": 1, "BCNCAR": 1, "BCNISD": 1, "BCNSPP": 1,
    "BOYLAT": 1, "BOYCAR": 1, "BOYISD": 1, "BOYSAW": 1, "BOYSPP": 1,
    # Hazards — same logic
    "OBSTRN": 1, "WRECKS": 1, "UWTROC": 1,
    # Soundings — extremely dense; push to top of band
    "SOUNDG": 1,
}

# Gap-fill config is loaded from enc-sources.yaml at runtime; see
# load_gap_fill_config() below.
GAP_FILL_CONFIG_FILE = "enc-sources.yaml"
GAP_FILL_DEFAULT_ZOOMS: Tuple[int, int] = (9, 10)


def load_gap_fill_config(
    config_path: Optional[Path] = None,
) -> Tuple[set, Tuple[int, int]]:
    """Load the gap-fill cell list and zoom range from enc-sources.yaml.
    Returns (empty set, default zooms) if the file is missing, has no
    gap_fills section, or pyyaml is unavailable."""
    if config_path is None:
        candidates = [
            Path.cwd() / GAP_FILL_CONFIG_FILE,
            Path(__file__).resolve().parent / GAP_FILL_CONFIG_FILE,
        ]
        config_path = next((p for p in candidates if p.exists()), None)
    if config_path is None or not config_path.exists():
        return set(), GAP_FILL_DEFAULT_ZOOMS
    try:
        import yaml  # type: ignore
    except ImportError:
        print(f"WARNING: pyyaml not installed; skipping gap-fill config",
              file=sys.stderr)
        return set(), GAP_FILL_DEFAULT_ZOOMS
    with open(config_path) as f:
        data = yaml.safe_load(f) or {}
    section = data.get("gap_fills") or {}
    cells = set(section.get("cells") or [])
    zr = section.get("zoom_range") or list(GAP_FILL_DEFAULT_ZOOMS)
    return cells, (int(zr[0]), int(zr[1]))


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class Source:
    path: Optional[Path]
    minzoom: int
    maxzoom: int
    label: str = ""
    geojson_dir: Optional[Path] = None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"s57-to-mbtiles v{__version__} — Convert S-57 ENC charts (.000) to vector MBTiles",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Single source:
  %(prog)s region03.zip
  %(prog)s region03.zip -o ne.mbtiles --minzoom 9 --maxzoom 16

Two-source split:
  %(prog)s region03.zip RI_detail.zip --split 12 -o ri.mbtiles

Multi-source explicit ranges:
  %(prog)s --sources region03.zip:9-11 RI.zip:12-16 -o merged.mbtiles

By-band (recommended for multi-state):
  %(prog)s CT_ENCs.zip RI_ENCs.zip NY_ENCs.zip --by-band -o ct-ri-ny.mbtiles

Skip GDAL (use existing GeoJSON):
  %(prog)s --geojson-dir ./data/geojson/band3/ --minzoom 11 --maxzoom 12
        """,
    )
    parser.add_argument("inputs", nargs="*",
                        help="ZIP file(s) or director(ies) containing .000 ENC files")
    parser.add_argument("--by-band", action="store_true",
                        help="Auto-group inputs by NOAA usage band.")
    parser.add_argument("--sources", nargs="+", metavar="FILE:MIN-MAX",
                        help="Explicit sources with zoom ranges.")
    parser.add_argument("--split", type=int, metavar="ZOOM",
                        help="Zoom split for two-input mode.")
    parser.add_argument("-o", "--output", help="Output .mbtiles filename")
    parser.add_argument("--output-dir",
                        help="Production directory to copy final .mbtiles to.")
    parser.add_argument("--minzoom", type=int, default=9)
    parser.add_argument("--maxzoom", type=int, default=16)
    parser.add_argument("--geojson-dir",
                        help="Skip GDAL, use existing GeoJSON directory")
    parser.add_argument("-j", "--jobs", type=int,
                        default=max(1, (os.cpu_count() or 2) // 2),
                        help="Parallel workers (default: half CPU count)")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    return parser


def setup_data_dir() -> Path:
    for sub in ("zips", "enc", "geojson", "merged", "tiles"):
        (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def resolve_output_name(args) -> str:
    if args.output:
        return args.output
    if args.inputs:
        stem = Path(args.inputs[0]).stem
        suffix = "-merged" if len(args.inputs) > 1 else ""
        return f"{stem}{suffix}.mbtiles"
    return "enc-chart.mbtiles"


def parse_source_spec(spec: str, default_min: int, default_max: int) -> Source:
    m = re.match(r'^(.+):(\d+)-(\d+)$', spec)
    if m:
        p = Path(m.group(1)).resolve()
        return Source(p, int(m.group(2)), int(m.group(3)), label=p.name)
    p = Path(spec).resolve()
    return Source(p, default_min, default_max, label=p.name)


def build_sources(args, parser) -> List[Source]:
    if args.sources:
        return [parse_source_spec(s, args.minzoom, args.maxzoom) for s in args.sources]
    if args.geojson_dir:
        gj = Path(args.geojson_dir).resolve()
        if not gj.is_dir():
            print(f"ERROR: {gj} is not a directory", file=sys.stderr)
            sys.exit(1)
        return [Source(None, args.minzoom, args.maxzoom, label="geojson", geojson_dir=gj)]
    if len(args.inputs) == 1:
        p = Path(args.inputs[0]).resolve()
        return [Source(p, args.minzoom, args.maxzoom, label=p.name)]
    if len(args.inputs) == 2 and args.split:
        return [
            Source(Path(args.inputs[0]).resolve(),
                   args.minzoom, args.split - 1, label=Path(args.inputs[0]).name),
            Source(Path(args.inputs[1]).resolve(),
                   args.split, args.maxzoom, label=Path(args.inputs[1]).name),
        ]
    if len(args.inputs) == 2:
        parser.error("Two inputs require --split or --by-band.")
    if len(args.inputs) > 2:
        parser.error("More than two inputs require --by-band or --sources.")
    parser.error("Provide at least one input, or use --sources / --geojson-dir")


def validate_sources(sources: List[Source]):
    for s in sources:
        if s.path and not s.path.exists():
            print(f"ERROR: {s.path} not found", file=sys.stderr)
            sys.exit(1)
        if s.minzoom > s.maxzoom:
            print(f"ERROR: minzoom {s.minzoom} > maxzoom {s.maxzoom}", file=sys.stderr)
            sys.exit(1)
    for i, a in enumerate(sources):
        for b in sources[i + 1:]:
            if a.minzoom <= b.maxzoom and b.minzoom <= a.maxzoom:
                print(f"WARNING: {a.label} overlaps {b.label}.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------

def find_container_runtime() -> Optional[str]:
    for cmd in ("podman", "docker"):
        if shutil.which(cmd):
            return cmd
    return None


def check_deps(need_gdal: bool = True) -> Tuple[bool, Optional[str]]:
    errors = []
    if not shutil.which("tippecanoe"):
        errors.append("tippecanoe not found.")
    if not shutil.which("tile-join"):
        errors.append("tile-join not found.")
    native_gdal = bool(shutil.which("ogr2ogr") and shutil.which("ogrinfo"))
    runtime = find_container_runtime()
    if need_gdal and not native_gdal and not runtime:
        errors.append("No GDAL found. Install ogr2ogr or podman/docker.")
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    if native_gdal:
        print("Using native GDAL")
    elif runtime:
        print(f"Using containerized GDAL via {runtime}")
    return native_gdal, runtime


def pull_image(runtime: str, image: str):
    check_cmd = "image exists" if runtime == "podman" else "image inspect"
    result = subprocess.run(
        [runtime] + check_cmd.split() + [image], capture_output=True
    )
    if result.returncode != 0:
        print(f"Pulling {image}...")
        subprocess.run([runtime, "pull", image], check=True)


# ---------------------------------------------------------------------------
# Freshness helpers (mtime-based skip for incremental rebuilds)
# ---------------------------------------------------------------------------

def output_is_fresh(output: Path, inputs: List[Path]) -> bool:
    """True if `output` exists, is non-empty, and is newer than every input."""
    if not output.exists() or output.stat().st_size <= 100:
        return False
    output_mtime = output.stat().st_mtime
    for p in inputs:
        if not p.exists():
            continue
        if p.stat().st_mtime > output_mtime:
            return False
    return True


def cell_outputs_fresh(enc_path: Path, geojson_dir: Path,
                       multi_file: bool) -> bool:
    """True if all GeoJSON outputs for this cell are newer than every source
    file (.000 + any .001..NNN ER updates in the same directory)."""
    cell_stem = enc_path.stem
    sources = list(enc_path.parent.glob(f"{cell_stem}.*"))
    if not sources:
        return False
    source_mtime = max(s.stat().st_mtime for s in sources)
    if multi_file:
        outputs = list(geojson_dir.glob(f"*_{cell_stem}.geojson"))
    else:
        outputs = list(geojson_dir.glob("*.geojson"))
    if not outputs:
        return False
    return all(o.stat().st_mtime >= source_mtime for o in outputs)


# ---------------------------------------------------------------------------
# Stage 1: Input staging
# ---------------------------------------------------------------------------

def stage_input(input_path: Path, enc_dir: Path, zips_dir: Path):
    enc_dir.mkdir(parents=True, exist_ok=True)
    if input_path.is_file() and zipfile.is_zipfile(input_path):
        zip_dest = zips_dir / input_path.name
        if not zip_dest.exists():
            shutil.copy2(input_path, zip_dest)
            print(f"Archived {input_path.name} -> {zip_dest}")
        print(f"Extracting {input_path.name}...")
        with zipfile.ZipFile(input_path, "r") as zf:
            zf.extractall(enc_dir)
    elif input_path.is_dir():
        shutil.copytree(input_path, enc_dir, dirs_exist_ok=True)
    else:
        print(f"ERROR: {input_path} is not a ZIP or directory", file=sys.stderr)
        sys.exit(1)


def find_enc_files(directory: Path) -> List[Path]:
    return sorted(directory.rglob("*.000"))


def enc_band(enc_file: Path) -> Optional[int]:
    m = re.match(r'^US(\d)', enc_file.stem, re.IGNORECASE)
    return int(m.group(1)) if m else None


def group_by_band(enc_files: List[Path]) -> Dict[int, List[Path]]:
    groups: Dict[int, List[Path]] = {}
    for f in enc_files:
        band = enc_band(f)
        groups.setdefault(band if band is not None else 0, []).append(f)
    return groups


# ---------------------------------------------------------------------------
# Stage 2: GDAL export
# ---------------------------------------------------------------------------

def export_to_geojson(
    enc_dir: Path,
    geojson_dir: Path,
    enc_files: List[Path],
    label: str = "",
    native_gdal: bool = True,
    runtime: Optional[str] = None,
    max_workers: int = 1,
) -> List[Path]:
    tag = f"[{label}] " if label else ""
    multi_file = len(enc_files) > 1

    # Per-cell freshness: only re-export cells whose source(s) are newer
    # than their existing GeoJSON outputs.
    cells_to_process = [
        f for f in enc_files
        if not cell_outputs_fresh(f, geojson_dir, multi_file)
    ]

    if not cells_to_process:
        existing = [f for f in geojson_dir.glob("*.geojson")
                    if f.stat().st_size > 100]
        print(f"{tag}GeoJSON fresh ({len(existing)} layers), skipping GDAL")
        return existing

    print(f"{tag}GDAL: converting {len(cells_to_process)}/{len(enc_files)} "
          f"cell(s) to GeoJSON...")

    if native_gdal:
        _export_native(enc_dir, geojson_dir, cells_to_process, multi_file,
                       tag, max_workers)
    else:
        _export_container(runtime, enc_dir, geojson_dir, cells_to_process,
                          multi_file, tag, label)

    valid = []
    for f in list(geojson_dir.glob("*.geojson")):
        if f.stat().st_size > 100:
            valid.append(f)
        else:
            f.unlink()

    print(f"{tag}Generated {len(valid)} GeoJSON layers")
    return valid


def _export_native(enc_dir, geojson_dir, cells_to_process, multi_file,
                   tag, max_workers):
    total = len(cells_to_process)
    done = [0]

    def process_enc(enc: Path):
        name = enc.stem
        result = subprocess.run(
            ["ogrinfo", "-so", str(enc)], capture_output=True, text=True)
        if result.returncode != 0:
            return

        layers = []
        for line in result.stdout.splitlines():
            m = re.match(r'^\d+:\s+(\S+)', line)
            if m:
                layers.append(m.group(1))

        for layer in layers:
            if layer in SKIP_LAYERS:
                continue
            outname = f"{layer}_{name}" if multi_file else layer
            outpath = geojson_dir / f"{outname}.geojson"
            if outpath.exists():
                outpath.unlink()
            cmd = ["ogr2ogr", "-f", "GeoJSON", "-oo", "LIST_AS_STRING=YES"]
            if layer == "SOUNDG":
                cmd.extend(["-oo", "SPLIT_MULTIPOINT=YES",
                            "-oo", "ADD_SOUNDG_DEPTH=YES"])
            cmd.extend([str(outpath), str(enc), layer])
            subprocess.run(cmd, capture_output=True)

        done[0] += 1
        print(f"{tag}[{done[0]}/{total}] {name}")

    if max_workers <= 1:
        for enc in cells_to_process:
            process_enc(enc)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(process_enc, enc): enc
                       for enc in cells_to_process}
            for future in as_completed(futures):
                exc = future.exception()
                if exc:
                    print(f"WARNING: {futures[future].name}: {exc}",
                          file=sys.stderr)

    print(f"{tag}Export complete")


def _export_container(runtime, enc_dir, geojson_dir, cells_to_process,
                      multi_file, tag, label):
    skip_case = "|".join(SKIP_LAYERS)
    name_template = "${layer}_${name}" if multi_file else "${layer}"

    # Pass explicit relative paths so the container processes only the
    # cells the freshness check decided are stale.
    rel_paths = [str(c.relative_to(enc_dir)) for c in cells_to_process]
    cells_arg = " ".join(rel_paths)

    script = f"""
set -e
cells="{cells_arg}"
count=$(echo "$cells" | wc -w)
i=0
for rel in $cells; do
  enc="/input/$rel"
  i=$((i + 1))
  name=$(basename "$enc" .000)
  echo "[$i/$count] $name"
  layers=$(ogrinfo -so "$enc" 2>/dev/null | grep -E '^[0-9]+:' | awk -F': ' '{{print $2}}' | awk '{{print $1}}')
  for layer in $layers; do
    case "$layer" in {skip_case}) continue ;; esac
    outname="{name_template}"
    rm -f "/output/$outname.geojson"
    if [ "$layer" = "SOUNDG" ]; then
      ogr2ogr -f GeoJSON -oo SPLIT_MULTIPOINT=YES -oo ADD_SOUNDG_DEPTH=YES \
        -oo LIST_AS_STRING=YES \
        "/output/$outname.geojson" "$enc" "$layer" 2>/dev/null || true
    else
      ogr2ogr -f GeoJSON -oo LIST_AS_STRING=YES \
        "/output/$outname.geojson" "$enc" "$layer" 2>/dev/null || true
    fi
  done
done
echo "Export complete"
"""

    result = subprocess.run(
        [runtime, "run", "--rm",
         "-v", f"{enc_dir}:/input:ro,Z",
         "-v", f"{geojson_dir}:/output:Z",
         GDAL_IMAGE, "sh", "-c", script],
        capture_output=False,
    )
    if result.returncode != 0:
        print(f"ERROR: GDAL export failed ({label})", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Stage 3: GeoJSON consolidation
# ---------------------------------------------------------------------------

def merge_geojson_layer(layer_name: str, source_files: List[Path],
                        output_path: Path):
    """Merge multiple GeoJSON files into one valid FeatureCollection.
    Uses streaming writes to keep memory low."""
    with open(output_path, "w") as out:
        out.write('{"type":"FeatureCollection","features":[\n')
        first = True
        for source_file in source_files:
            try:
                with open(source_file) as inp:
                    fc = json.load(inp)
            except (json.JSONDecodeError, OSError):
                continue
            for feat in fc.get("features", []):
                if not first:
                    out.write(",\n")
                json.dump(feat, out)
                first = False
        out.write("\n]}\n")


def consolidate_geojson(geojson_dir: Path, merged_dir: Path,
                        max_workers: int = 1) -> List[Path]:
    """Group geojson files by layer name and merge into one file per layer.
    Returns list of merged file paths."""
    merged_dir.mkdir(parents=True, exist_ok=True)

    geojson_files = [f for f in sorted(geojson_dir.glob("*.geojson"))
                     if f.stat().st_size > 100]
    if not geojson_files:
        return []

    # Group by layer name
    layer_groups: Dict[str, List[Path]] = {}
    for f in geojson_files:
        layer_name = f.stem.split("_")[0] if "_" in f.stem else f.stem
        layer_groups.setdefault(layer_name, []).append(f)

    # Per-layer freshness pre-pass
    fresh: List[Path] = []
    stale: List[Tuple[str, List[Path], Path]] = []
    for layer_name, files in layer_groups.items():
        out_path = merged_dir / f"{layer_name}.geojson"
        if output_is_fresh(out_path, files):
            fresh.append(out_path)
        else:
            stale.append((layer_name, files, out_path))

    if not stale:
        print(f"  All {len(layer_groups)} merged layers fresh, skipping")
        return sorted(fresh)

    print(f"  Consolidating {len(stale)}/{len(layer_groups)} layers "
          f"(others fresh)...")

    def merge_one(item):
        layer_name, files, out_path = item
        if len(files) == 1:
            shutil.copy2(files[0], out_path)
        else:
            merge_geojson_layer(layer_name, files, out_path)
        return out_path

    if max_workers <= 1:
        results = [merge_one(item) for item in stale]
    else:
        results = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(merge_one, item): item[0]
                       for item in stale}
            for future in as_completed(futures):
                exc = future.exception()
                if exc:
                    print(f"WARNING: merge {futures[future]}: {exc}",
                          file=sys.stderr)
                else:
                    results.append(future.result())

    print(f"  Consolidated {len(results)} layers (+ {len(fresh)} fresh)")
    return sorted(fresh + results)


# ---------------------------------------------------------------------------
# Stage 4: tippecanoe (one invocation per band, full zoom range)
# ---------------------------------------------------------------------------

def run_tippecanoe_for_source(
    merged_dir: Path,
    tile_dir: Path,
    stem: str,
    minzoom: int,
    maxzoom: int,
    max_workers: int = 1,  # unused; tippecanoe handles its own threading
) -> Optional[Path]:
    """Run a single tippecanoe over [minzoom, maxzoom] using merged GeoJSON.
    Each layer carries its own minzoom from LAYER_MIN_ZOOM_OFFSET via the
    JSON layer-spec form of -L. Returns the produced .mbtiles path, or None
    if there's nothing to build."""
    merged_files = [f for f in sorted(merged_dir.glob("*.geojson"))
                    if f.stat().st_size > 100]
    if not merged_files:
        print(f"WARNING: No GeoJSON in {merged_dir}, skipping", file=sys.stderr)
        return None

    final = tile_dir / f"{stem}.mbtiles"

    if output_is_fresh(final, merged_files):
        print(f"  [{stem}] z{minzoom}-{maxzoom}: fresh "
              f"({final.stat().st_size / 1048576:.1f} MB), skipping")
        return final

    # Build per-layer JSON layer specs with per-layer minzoom
    layer_args = []
    for f in merged_files:
        layer_name = f.stem
        layer_min = min(minzoom + LAYER_MIN_ZOOM_OFFSET.get(layer_name, 0),
                        maxzoom)
        spec = {"file": str(f), "layer": layer_name, "minzoom": layer_min}
        layer_args.extend(["-L", json.dumps(spec)])

    print(f"tippecanoe [{stem}]: {len(merged_files)} layers, "
          f"z{minzoom}-{maxzoom}")

    tmp = (tile_dir / f".tmp-{stem}").resolve()
    tmp.mkdir(exist_ok=True)

    cmd = [
        "tippecanoe",
        "-o", str(final),
        "-Z", str(minzoom), "-z", str(maxzoom),
        "--no-tile-size-limit",
        "--no-feature-limit",
        "--no-simplification",
        "--no-tiny-polygon-reduction",
        "--detect-shared-borders",
        "--buffer=80",
        "--force",
        "--temporary-directory", str(tmp),
        *layer_args,
    ]
    result = subprocess.run(cmd)
    shutil.rmtree(tmp, ignore_errors=True)

    if result.returncode != 0 or not final.exists() or final.stat().st_size == 0:
        if final.exists():
            final.unlink()
        raise RuntimeError(f"tippecanoe failed for {stem}")

    _patch_metadata(final, stem)
    print(f"  [{stem}] done ({final.stat().st_size / 1048576:.1f} MB)")
    return final


def _patch_metadata(mbtiles_path: Path, name: str):
    db = sqlite3.connect(str(mbtiles_path))
    db.execute("CREATE TABLE IF NOT EXISTS metadata (name text, value text)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS name ON metadata (name)")
    db.execute("INSERT OR REPLACE INTO metadata (name, value) "
               "VALUES ('type', 'S-57')")
    db.execute("INSERT OR REPLACE INTO metadata (name, value) "
               "VALUES ('name', ?)", (name,))
    db.execute("INSERT OR REPLACE INTO metadata (name, value) "
               "VALUES ('description', ?)", (name,))
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Stage 5: tile-join merge
# ---------------------------------------------------------------------------

def merge_mbtiles(tile_files: List[Path], output_path: Path, final_name: str):
    print(f"\nMerging {len(tile_files)} tile set(s) with tile-join...")
    cmd = [
        "tile-join",
        "--no-tile-size-limit",
        "--force",
        "-o", str(output_path),
        *[str(f) for f in tile_files],
    ]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print("ERROR: tile-join failed", file=sys.stderr)
        sys.exit(1)
    _patch_metadata(output_path, final_name)
    print(f"Merged -> {output_path} ({output_path.stat().st_size / 1048576:.1f} MB)")


# ---------------------------------------------------------------------------
# Gap fill: render specific band 3 cells at band 2 zooms
# ---------------------------------------------------------------------------

def process_gap_fill(
    data_dir: Path,
    minzoom: int,
    maxzoom: int,
    max_workers: int,
) -> Optional[Path]:
    """Render the configured gap-fill cells at the configured zoom range to
    cover NOAA's band 2 coverage holes. Reuses the band 3 GeoJSON already
    produced by process_band. Returns the gap-fill mbtiles path, or None when
    there's nothing to do."""
    gap_cells, gap_zooms = load_gap_fill_config()
    if not gap_cells:
        return None

    effective_min = max(gap_zooms[0], minzoom)
    effective_max = min(gap_zooms[1], maxzoom)
    if effective_min > effective_max:
        return None

    band3_geojson = data_dir / "geojson" / "band3"
    if not band3_geojson.is_dir():
        return None

    present_cells = sorted(
        c for c in gap_cells
        if any(band3_geojson.glob(f"*_{c}.geojson"))
    )
    if not present_cells:
        return None

    print(f"\n-- Gap fill: {len(present_cells)} band-3 cell(s) at "
          f"z{effective_min}-{effective_max} --")
    print(f"   Cells: {', '.join(present_cells)}")

    # Group the per-cell geojson files by layer name (matches the convention
    # used by consolidate_geojson: filename is "LAYER_CELLSTEM.geojson").
    layer_groups: Dict[str, List[Path]] = {}
    for cell in present_cells:
        for f in band3_geojson.glob(f"*_{cell}.geojson"):
            if f.stat().st_size <= 100:
                continue
            layer_name = f.stem.split("_")[0]
            layer_groups.setdefault(layer_name, []).append(f)

    if not layer_groups:
        return None

    gap_merged_dir = data_dir / "merged" / "gapfill"
    gap_merged_dir.mkdir(parents=True, exist_ok=True)

    for layer_name, files in layer_groups.items():
        out_path = gap_merged_dir / f"{layer_name}.geojson"
        if output_is_fresh(out_path, files):
            continue
        if len(files) == 1:
            shutil.copy2(files[0], out_path)
        else:
            merge_geojson_layer(layer_name, files, out_path)

    tile_dir = data_dir / "tiles"
    return run_tippecanoe_for_source(
        gap_merged_dir, tile_dir, "gapfill",
        effective_min, effective_max, max_workers=max_workers)


# ---------------------------------------------------------------------------
# Band pipeline (stages 2-4 for one band)
# ---------------------------------------------------------------------------

def process_band(
    band: int,
    enc_files: List[Path],
    data_dir: Path,
    effective_min: int,
    effective_max: int,
    desc: str,
    scale: str,
    native_gdal: bool,
    runtime: Optional[str],
    max_workers: int,
) -> Optional[Path]:
    """Run stages 2-4 for a single band. Returns the band's .mbtiles path."""
    label = f"band{band}-{desc}"
    print(f"\n-- Band {band}: {desc} ({scale})  z{effective_min}-{effective_max} --")

    enc_base = data_dir / "enc"
    geojson_base = data_dir / "geojson"
    merged_base = data_dir / "merged"
    tile_dir = data_dir / "tiles"

    band_enc_dir = enc_base / f"band{band}"
    band_geojson_dir = geojson_base / f"band{band}"
    band_merged_dir = merged_base / f"band{band}"

    band_enc_dir.mkdir(parents=True, exist_ok=True)
    band_geojson_dir.mkdir(parents=True, exist_ok=True)

    # Copy each cell's .000 base AND any .001..NNN ER update files.
    # Without the update files, ogr2ogr applies only the base — incremental
    # NOAA chart updates would be silently dropped.
    band_cells: List[Path] = []
    for enc_file in enc_files:
        cell_stem = enc_file.stem
        for src in enc_file.parent.glob(f"{cell_stem}.*"):
            dest = band_enc_dir / src.name
            if (not dest.exists()
                    or src.stat().st_mtime > dest.stat().st_mtime):
                shutil.copy2(src, dest)
        band_cells.append(band_enc_dir / enc_file.name)

    # Stage 2: GDAL export
    export_to_geojson(
        band_enc_dir, band_geojson_dir, band_cells, label=label,
        native_gdal=native_gdal, runtime=runtime, max_workers=max_workers)

    # Stage 3: Consolidate
    consolidate_geojson(band_geojson_dir, band_merged_dir,
                        max_workers=max_workers)

    # Stage 4: one tippecanoe for the band
    return run_tippecanoe_for_source(
        band_merged_dir, tile_dir, label,
        effective_min, effective_max, max_workers=max_workers)


# ---------------------------------------------------------------------------
# By-band orchestration (parallel across bands)
# ---------------------------------------------------------------------------

def process_by_band(
    inputs: List[Path],
    data_dir: Path,
    minzoom: int,
    maxzoom: int,
    native_gdal: bool,
    runtime: Optional[str],
    max_workers: int,
) -> List[Path]:
    print("\n-- By-band mode ---------------------------------------------------")

    # Stage 1: stage all inputs
    staging_dir = data_dir / "enc" / "all"
    staging_dir.mkdir(parents=True, exist_ok=True)
    zips_dir = data_dir / "zips"
    for i, p in enumerate(inputs):
        dest = staging_dir / f"input{i}"
        if dest.exists() and list(dest.rglob("*.000")):
            print(f"Input {i} already staged, skipping")
        else:
            stage_input(p, dest, zips_dir)

    all_enc = find_enc_files(staging_dir)
    print(f"Total ENC files found: {len(all_enc)}")

    by_band = group_by_band(all_enc)

    # Warn about non-NOAA files
    if 0 in by_band:
        print(f"WARNING: {len(by_band[0])} file(s) don't match NOAA naming, "
              f"will be skipped.", file=sys.stderr)

    # Print band inventory
    print("\nBand inventory:")
    for band in sorted(b for b in by_band if b > 0):
        zoom_min, zoom_max, desc, scale = BAND_ZOOM.get(
            band, (None, None, "unknown", "?"))
        skipped = ""
        if zoom_min is not None and (zoom_max < minzoom or zoom_min > maxzoom):
            skipped = "  <- outside zoom range, skipping"
        print(f"  Band {band} ({desc}, {scale}): {len(by_band[band])} file(s)"
              f"  z{zoom_min}-{zoom_max}{skipped}")

    # Build list of bands to process
    band_tasks = []
    for band in sorted(b for b in by_band if b > 0):
        if band not in BAND_ZOOM:
            continue
        zoom_min, zoom_max, desc, scale = BAND_ZOOM[band]
        effective_min = max(zoom_min, minzoom)
        effective_max = min(zoom_max, maxzoom)
        if effective_min > effective_max:
            continue
        band_tasks.append((band, by_band[band], effective_min, effective_max,
                           desc, scale))

    # Run all bands — each band runs stages 2→3→4 sequentially,
    # but bands run concurrently via threads.
    # Each band's internal stages use subprocess calls that the OS
    # schedules across cores.
    band_tiles: Dict[int, Path] = {}

    if len(band_tasks) <= 1 or max_workers <= 1:
        for band, files, emin, emax, desc, scale in band_tasks:
            result = process_band(
                band, files, data_dir, emin, emax, desc, scale,
                native_gdal, runtime, max_workers)
            if result is not None:
                band_tiles[band] = result
    else:
        with ThreadPoolExecutor(max_workers=min(len(band_tasks),
                                                max_workers)) as pool:
            futures = {}
            for band, files, emin, emax, desc, scale in band_tasks:
                f = pool.submit(process_band, band, files, data_dir,
                                emin, emax, desc, scale,
                                native_gdal, runtime, max_workers)
                futures[f] = band
            for future in as_completed(futures):
                exc = future.exception()
                if exc:
                    print(f"ERROR: band {futures[future]}: {exc}",
                          file=sys.stderr)
                    sys.exit(1)
                result = future.result()
                if result is not None:
                    band_tiles[futures[future]] = result

    if not band_tiles:
        print("ERROR: No tiles produced", file=sys.stderr)
        sys.exit(1)

    gap_tiles_path = process_gap_fill(data_dir, minzoom, maxzoom, max_workers)

    # Coarse → fine ordering by band number (band 1 = overview, band 6 = berthing)
    sorted_bands = sorted(band_tiles)
    ordered = [band_tiles[b] for b in sorted_bands]
    if gap_tiles_path is not None:
        # Slot between band 2 and band 3 so band 3 detail wins on overlap.
        insert_idx = next((i for i, b in enumerate(sorted_bands) if b > 2),
                          len(ordered))
        ordered.insert(insert_idx, gap_tiles_path)
    return ordered


# ---------------------------------------------------------------------------
# Per-source pipeline (standard modes)
# ---------------------------------------------------------------------------

def process_source(
    source: Source,
    data_dir: Path,
    idx: int,
    native_gdal: bool,
    runtime: Optional[str],
    max_workers: int,
) -> Optional[Path]:
    label = source.label or f"source{idx}"
    safe_label = re.sub(r'[^\w\-.]', '_', label)

    enc_dir = data_dir / "enc" / safe_label
    geojson_dir = data_dir / "geojson" / safe_label
    merged_dir = data_dir / "merged" / safe_label
    tile_dir = data_dir / "tiles"

    enc_dir.mkdir(parents=True, exist_ok=True)
    geojson_dir.mkdir(parents=True, exist_ok=True)

    if source.geojson_dir:
        geojson_dir = source.geojson_dir
        count = len([f for f in geojson_dir.glob("*.geojson")
                     if f.stat().st_size > 100])
        print(f"\n[{label}] Using GeoJSON: {geojson_dir} ({count} layers)")
    else:
        print(f"\n[{label}] z{source.minzoom}-{source.maxzoom}  {source.path}")
        stage_input(source.path, enc_dir, data_dir / "zips")
        enc_files = find_enc_files(enc_dir)
        if not enc_files:
            print(f"ERROR: No .000 files in {source.path}", file=sys.stderr)
            sys.exit(1)
        print(f"Found {len(enc_files)} ENC file(s)")
        export_to_geojson(
            enc_dir, geojson_dir, enc_files, label=label,
            native_gdal=native_gdal, runtime=runtime,
            max_workers=max_workers)

    # Stage 3: Consolidate
    consolidated = consolidate_geojson(geojson_dir, merged_dir,
                                       max_workers=max_workers)
    # Use merged dir if consolidation produced files, else raw geojson
    input_dir = merged_dir if consolidated else geojson_dir

    return run_tippecanoe_for_source(
        input_dir, tile_dir, f"s{idx}",
        source.minzoom, source.maxzoom, max_workers=max_workers)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = build_parser()
    args = parser.parse_args()

    data_dir = setup_data_dir()
    out_name = resolve_output_name(args)
    tiles_path = data_dir / "tiles" / out_name

    prod_path = None
    if args.output_dir:
        prod_dir = Path(args.output_dir).resolve()
        prod_dir.mkdir(parents=True, exist_ok=True)
        prod_path = prod_dir / out_name

    native_gdal, runtime = check_deps(
        need_gdal=not args.geojson_dir)
    if not native_gdal and runtime:
        pull_image(runtime, GDAL_IMAGE)

    if args.by_band:
        if args.geojson_dir:
            parser.error("--geojson-dir cannot be used with --by-band")
        if not args.inputs:
            parser.error("--by-band requires at least one input")

        input_paths = [Path(p).resolve() for p in args.inputs]
        for p in input_paths:
            if not p.exists():
                print(f"ERROR: {p} not found", file=sys.stderr)
                sys.exit(1)

        # process_by_band returns one .mbtiles per band, ordered coarse → fine
        tile_files = process_by_band(
            input_paths, data_dir, args.minzoom, args.maxzoom,
            native_gdal, runtime, args.jobs)

        if len(tile_files) == 1:
            shutil.copy2(tile_files[0], tiles_path)
            _patch_metadata(tiles_path, tiles_path.stem)
        else:
            merge_mbtiles(tile_files, tiles_path, tiles_path.stem)

        print(f"\nSummary (by-band):")
        print(f"  Inputs: {', '.join(p.name for p in input_paths)}")
        for path in tile_files:
            print(f"  {path.name} "
                  f"({path.stat().st_size / 1048576:.1f} MB)")

    else:
        sources = build_sources(args, parser)
        validate_sources(sources)

        # Pair each source's mbtiles with its minzoom for coarse→fine sort
        source_tiles: List[Tuple[int, Path]] = []
        for i, source in enumerate(sources):
            result = process_source(
                source, data_dir, i + 1,
                native_gdal, runtime, args.jobs)
            if result is not None:
                source_tiles.append((source.minzoom, result))

        if not source_tiles:
            print("ERROR: No tiles produced", file=sys.stderr)
            sys.exit(1)

        source_tiles.sort(key=lambda x: x[0])
        tile_files = [p for _, p in source_tiles]

        if len(tile_files) == 1:
            shutil.copy2(tile_files[0], tiles_path)
            _patch_metadata(tiles_path, tiles_path.stem)
        else:
            merge_mbtiles(tile_files, tiles_path, tiles_path.stem)

        print(f"\nSummary:")
        for i, s in enumerate(sources):
            print(f"  Source {i+1}: {s.label}  z{s.minzoom}-{s.maxzoom}")

    size_mb = tiles_path.stat().st_size / 1048576
    print(f"  Tiles: {tiles_path} ({size_mb:.1f} MB)")
    if prod_path:
        shutil.copy2(tiles_path, prod_path)
        print(f"  Copied: {prod_path}")
    print(f"\nAll data preserved in {data_dir.resolve()}/")


if __name__ == "__main__":
    main()
