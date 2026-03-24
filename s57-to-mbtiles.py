#!/usr/bin/env python3 -u
"""
s57-to-mbtiles.py — Convert S-57 ENC charts to vector MBTiles

Takes one or more ZIP files (or directories) of S-57 ENC files (.000) and
produces a single merged vector MBTiles file.

─────────────────────────────────────────────────────────────────────────────
MODES
─────────────────────────────────────────────────────────────────────────────

SINGLE SOURCE (backward compatible):
  %(prog)s NY_ENCs.zip
  %(prog)s NY_ENCs.zip -o ny-charts.mbtiles --minzoom 9 --maxzoom 16

TWO-SOURCE MERGE (coarse + detail):
  %(prog)s region03.zip RI_detail.zip --split 12 -o ri-merged.mbtiles

GENERAL MULTI-SOURCE (explicit zoom ranges per source):
  %(prog)s --sources region03.zip:9-11 RI_detail.zip:12-16 -o merged.mbtiles
  %(prog)s --sources overview.zip:7-9 regional.zip:10-12 detail.zip:13-16

BY-BAND (recommended for multi-state regions):
  %(prog)s CT_ENCs.zip RI_ENCs.zip NY_ENCs.zip --by-band -o ct-ri-ny.mbtiles

  Automatically groups all .000 files by NOAA usage band (US1*-US6*) and
  assigns each band the appropriate zoom range:

    Band 1  overview  ~1:3.5M    z7-8
    Band 2  general   ~1:700k    z9-10
    Band 3  coastal   ~1:90k     z11-12
    Band 4  approach  ~1:22k     z13-14
    Band 5  harbour   ~1:8k      z15-16
    Band 6  berthing  ~1:3k      z17-18

  State boundaries are irrelevant: all CT and RI approach charts go into
  the same band-4 tippecanoe run, so no source steps on another.

SKIP GDAL (use existing GeoJSON):
  %(prog)s --geojson-dir ./geojson/ --minzoom 9 --maxzoom 16

─────────────────────────────────────────────────────────────────────────────
DATA DIRECTORY

All intermediate and final artifacts are stored in ./data/:

  data/
  ├── zips/       Original input ZIPs (preserved)
  ├── enc/        Extracted .000 files
  ├── geojson/    GeoJSON from GDAL export
  └── tiles/      Per-zoom and final merged .mbtiles

Nothing is deleted between runs. Tippecanoe skips zoom levels where a
non-empty .mbtiles already exists (resume-friendly).

Use --output-dir to copy the final .mbtiles to a production location
(e.g. ~/.signalk/charts-simple/).

─────────────────────────────────────────────────────────────────────────────
PIPELINE (per band or source)
  1. Copy ZIPs to data/zips/, extract to data/enc/
  2. Find .000 ENC files (grouped by band in --by-band mode)
  3. ogr2ogr (via podman/docker GDAL container) -> data/geojson/
  4. tippecanoe (native) -> per-zoom .mbtiles in data/tiles/
  5. tile-join -> merged .mbtiles in data/tiles/

Requirements: podman or docker (GDAL), tippecanoe + tile-join (native)
─────────────────────────────────────────────────────────────────────────────
"""

import argparse
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

GDAL_IMAGE = "ghcr.io/osgeo/gdal:alpine-small-latest"
SKIP_LAYERS = {"DSID", "C_AGGR", "C_ASSO", "Generic"}
DATA_DIR = Path("data")

# NOAA ENC usage band -> (minzoom, maxzoom, description, approx scale)
# Filename convention: US{band}{region_code}NNN_M.000
# e.g. US5CT03M.000 -> band 5, Connecticut
BAND_ZOOM: Dict[int, Tuple[int, int, str, str]] = {
    1: (7,  8,  "overview",  "~1:3,500,000"),
    2: (9,  10, "general",   "~1:700,000"),
    3: (11, 12, "coastal",   "~1:90,000"),
    4: (13, 14, "approach",  "~1:22,000"),
    5: (15, 16, "harbour",   "~1:8,000"),
    6: (17, 18, "berthing",  "~1:3,000"),
}


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
        description="Convert S-57 ENC charts (.000) to vector MBTiles",
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
  %(prog)s CT_ENCs.zip RI_ENCs.zip --by-band --minzoom 9 --maxzoom 16 -o region.mbtiles

Skip GDAL (use existing GeoJSON):
  %(prog)s --geojson-dir ./data/geojson/band3/ --minzoom 11 --maxzoom 12

All artifacts are stored in ./data/ and preserved across runs.
        """,
    )
    parser.add_argument("inputs", nargs="*",
                        help="ZIP file(s) or director(ies) containing .000 ENC files")
    parser.add_argument("--by-band", action="store_true",
                        help="Auto-group inputs by NOAA usage band (US1*-US6*) and assign "
                             "each band its correct zoom range. Recommended for multi-state regions.")
    parser.add_argument("--sources", nargs="+", metavar="FILE:MIN-MAX",
                        help="Explicit sources with zoom ranges, e.g. region03.zip:9-11 ri.zip:12-16")
    parser.add_argument("--split", type=int, metavar="ZOOM",
                        help="Zoom split when two inputs given: input1=minzoom-SPLIT-1, input2=SPLIT-maxzoom")
    parser.add_argument("-o", "--output", help="Output .mbtiles filename")
    parser.add_argument("--output-dir",
                        help="Production directory to copy final .mbtiles to "
                             "(e.g. ~/.signalk/charts-simple/)")
    parser.add_argument("--minzoom", type=int, default=9,
                        help="Minimum zoom (default: 9). In --by-band mode clips band ranges from below.")
    parser.add_argument("--maxzoom", type=int, default=16,
                        help="Maximum zoom (default: 16). In --by-band mode clips band ranges from above.")
    parser.add_argument("--geojson-dir", help="Skip GDAL, use existing GeoJSON directory")
    parser.add_argument("-j", "--jobs", type=int,
                        default=max(1, os.cpu_count() // 2),
                        help="Parallel tippecanoe jobs (default: half of CPU count)")
    return parser


def setup_data_dir() -> Path:
    """Create the persistent data directory structure."""
    for sub in ("zips", "enc", "geojson", "tiles"):
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
    """Resolve CLI arguments into a list of Sources (all modes except by-band)."""
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
        parser.error(
            "Two inputs given without --split or --by-band.\n"
            "  --split ZOOM  assigns zoom ranges manually.\n"
            "  --by-band     auto-splits by NOAA usage band (recommended)."
        )
    if len(args.inputs) > 2:
        parser.error(
            "More than two positional inputs require --by-band or --sources.\n"
            "  --by-band is recommended for multi-state ENC collections."
        )
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
                print(
                    f"WARNING: {a.label} (z{a.minzoom}-{a.maxzoom}) overlaps "
                    f"{b.label} (z{b.minzoom}-{b.maxzoom}). Later source wins.",
                    file=sys.stderr,
                )


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------

def find_container_runtime() -> Optional[str]:
    for cmd in ("podman", "docker"):
        if shutil.which(cmd):
            return cmd
    return None


def check_deps(need_gdal: bool = True) -> Tuple[bool, Optional[str]]:
    """
    Check required tools. Returns (native_gdal, container_runtime).
    native_gdal is True if ogr2ogr and ogrinfo are installed locally.
    """
    errors = []
    if not shutil.which("tippecanoe"):
        errors.append("tippecanoe not found.")
    if not shutil.which("tile-join"):
        errors.append("tile-join not found (ships with tippecanoe).")

    native_gdal = bool(shutil.which("ogr2ogr") and shutil.which("ogrinfo"))
    runtime = find_container_runtime()

    if need_gdal and not native_gdal and not runtime:
        errors.append("No GDAL found. Install ogr2ogr natively or install podman/docker.")
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if native_gdal:
        print("Using native GDAL (ogr2ogr)")
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
# Input handling
# ---------------------------------------------------------------------------

def stage_input(input_path: Path, enc_dir: Path, zips_dir: Path):
    """
    Stage input into the data directory.
    - ZIPs are copied to zips_dir (preserved) and extracted to enc_dir.
    - Directories are copied to enc_dir.
    """
    enc_dir.mkdir(parents=True, exist_ok=True)
    if input_path.is_file() and zipfile.is_zipfile(input_path):
        zip_dest = zips_dir / input_path.name
        if not zip_dest.exists():
            shutil.copy2(input_path, zip_dest)
            print(f"Archived {input_path.name} -> {zip_dest}")
        extract_zip(input_path, enc_dir)
    elif input_path.is_dir():
        shutil.copytree(input_path, enc_dir, dirs_exist_ok=True)
    else:
        print(f"ERROR: {input_path} is not a ZIP or directory", file=sys.stderr)
        sys.exit(1)


def extract_zip(zip_path: Path, target_dir: Path):
    print(f"Extracting {zip_path.name}...")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(target_dir)


def find_enc_files(directory: Path) -> List[Path]:
    return sorted(directory.rglob("*.000"))


def enc_band(enc_file: Path) -> Optional[int]:
    """
    Extract NOAA usage band from ENC filename.
    NOAA convention: US{band}{region}NNN_M.000
    e.g. US5CT03M.000 -> band 5
    Returns None if filename doesn't match.
    """
    m = re.match(r'^US(\d)', enc_file.stem, re.IGNORECASE)
    return int(m.group(1)) if m else None


def group_by_band(enc_files: List[Path]) -> Dict[int, List[Path]]:
    """Group ENC files by NOAA usage band. Non-matching files go under key 0."""
    groups: Dict[int, List[Path]] = {}
    for f in enc_files:
        band = enc_band(f)
        groups.setdefault(band if band is not None else 0, []).append(f)
    return groups


# ---------------------------------------------------------------------------
# Pipeline stage 1: GDAL → GeoJSON
# ---------------------------------------------------------------------------

def export_to_geojson(
    enc_dir: Path,
    geojson_dir: Path,
    enc_files: List[Path],
    label: str = "",
    native_gdal: bool = True,
    runtime: Optional[str] = None,
) -> List[Path]:
    tag = f"[{label}] " if label else ""
    print(f"{tag}GDAL: converting {len(enc_files)} ENC file(s) to GeoJSON...")

    if native_gdal:
        _export_native(enc_dir, geojson_dir, enc_files, tag)
    else:
        _export_container(runtime, enc_dir, geojson_dir, enc_files, tag, label)

    valid = []
    for f in list(geojson_dir.glob("*.geojson")):
        if f.stat().st_size > 100:
            valid.append(f)
        else:
            f.unlink()

    print(f"{tag}Generated {len(valid)} GeoJSON layers")
    return valid


def _export_native(
    enc_dir: Path,
    geojson_dir: Path,
    enc_files: List[Path],
    tag: str,
):
    """Export using locally installed ogr2ogr."""
    multi_file = len(enc_files) > 1
    actual_files = sorted(enc_dir.rglob("*.000"))

    for i, enc in enumerate(actual_files, 1):
        name = enc.stem
        print(f"{tag}[{i}/{len(actual_files)}] {name}")

        # Get layer list
        result = subprocess.run(
            ["ogrinfo", "-so", str(enc)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            continue

        layers = []
        for line in result.stdout.splitlines():
            m = re.match(r'^\d+:\s+(\S+)', line)
            if m:
                layers.append(m.group(1))

        for layer in layers:
            if layer in SKIP_LAYERS:
                continue
            if multi_file:
                outname = f"{layer}_{name}"
            else:
                outname = layer

            outpath = geojson_dir / f"{outname}.geojson"
            cmd = ["ogr2ogr", "-f", "GeoJSON", "-oo", "LIST_AS_STRING=YES"]
            if layer == "SOUNDG":
                cmd.extend(["-oo", "SPLIT_MULTIPOINT=YES", "-oo", "ADD_SOUNDG_DEPTH=YES"])
            cmd.extend([str(outpath), str(enc), layer])

            subprocess.run(cmd, capture_output=True)

    print(f"{tag}Export complete")


def _export_container(
    runtime: str,
    enc_dir: Path,
    geojson_dir: Path,
    enc_files: List[Path],
    tag: str,
    label: str,
):
    """Export using containerized GDAL (podman/docker)."""
    multi_file = len(enc_files) > 1
    skip_case = "|".join(SKIP_LAYERS)
    name_template = "${layer}_${name}" if multi_file else "${layer}"

    script = f"""
set -e
enc_files=$(find /input -name '*.000' -type f)
count=$(echo "$enc_files" | wc -l)
i=0
for enc in $enc_files; do
  i=$((i + 1))
  name=$(basename "$enc" .000)
  echo "[$i/$count] $name"
  layers=$(ogrinfo -so "$enc" 2>/dev/null | grep -E '^[0-9]+:' | awk -F': ' '{{print $2}}' | awk '{{print $1}}')
  for layer in $layers; do
    case "$layer" in {skip_case}) continue ;; esac
    outname="{name_template}"
    if [ "$layer" = "SOUNDG" ]; then
      ogr2ogr -f GeoJSON \
        -oo SPLIT_MULTIPOINT=YES -oo ADD_SOUNDG_DEPTH=YES -oo LIST_AS_STRING=YES \
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
        [
            runtime, "run", "--rm",
            "-v", f"{enc_dir}:/input:ro,Z",
            "-v", f"{geojson_dir}:/output:Z",
            GDAL_IMAGE, "sh", "-c", script,
        ],
        capture_output=False,
    )
    if result.returncode != 0:
        print(f"ERROR: GDAL export failed{' (' + label + ')' if label else ''}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Pipeline stage 2: tippecanoe → per-zoom .mbtiles
# ---------------------------------------------------------------------------

def run_tippecanoe(
    geojson_dir: Path,
    tile_dir: Path,
    stem: str,
    minzoom: int,
    maxzoom: int,
    max_workers: int = 1,
) -> List[Tuple[int, Path]]:
    geojson_files = [f for f in sorted(geojson_dir.glob("*.geojson")) if f.stat().st_size > 100]
    if not geojson_files:
        print(f"WARNING: No valid GeoJSON in {geojson_dir}, skipping", file=sys.stderr)
        return []

    layer_args = []
    for f in geojson_files:
        layer_name = f.stem.split("_")[0] if "_" in f.stem else f.stem
        layer_args.extend(["-L", f"{layer_name}:{f}"])

    print(f"tippecanoe [{stem}]: {len(geojson_files)} layers, z{minzoom}-{maxzoom}"
          f"  ({max_workers} worker{'s' if max_workers > 1 else ''})")

    def build_zoom(z: int) -> Tuple[int, Path]:
        final = tile_dir / f"{stem}_z{z}.mbtiles"
        if final.exists() and final.stat().st_size > 0:
            print(f"  z{z}: exists ({final.stat().st_size / 1048576:.1f} MB), skipping")
            return (z, final)

        # Each zoom gets its own temp dir to avoid conflicts
        tmp_dir = tile_dir / f".tmp-{stem}-z{z}"
        tmp_dir.mkdir(exist_ok=True)

        print(f"  z{z}: running...")
        cmd = [
            "tippecanoe",
            "-o", str(final),
            "-z", str(z), "-Z", str(z),
            "--no-tile-size-limit",
            "--no-feature-limit",
            "--no-simplification",
            "--buffer=80",
            "--force",
            "--temporary-directory", str(tmp_dir),
            *layer_args,
        ]
        result = subprocess.run(cmd, capture_output=(max_workers > 1))
        shutil.rmtree(tmp_dir, ignore_errors=True)

        if result.returncode != 0 or not final.exists() or final.stat().st_size == 0:
            if final.exists():
                final.unlink()
            raise RuntimeError(f"tippecanoe failed on z{z}")

        _patch_metadata(final, f"{stem} z{z}")
        print(f"  z{z}: done ({final.stat().st_size / 1048576:.1f} MB)")
        return (z, final)

    zooms = list(range(minzoom, maxzoom + 1))

    if max_workers <= 1:
        results = [build_zoom(z) for z in zooms]
    else:
        results = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(build_zoom, z): z for z in zooms}
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except RuntimeError as e:
                    print(f"ERROR: {e}", file=sys.stderr)
                    sys.exit(1)

    return sorted(results, key=lambda x: x[0])


def _patch_metadata(mbtiles_path: Path, name: str):
    db = sqlite3.connect(str(mbtiles_path))
    db.execute("CREATE TABLE IF NOT EXISTS metadata (name text, value text)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS name ON metadata (name)")
    db.execute("INSERT OR REPLACE INTO metadata (name, value) VALUES ('type', 'S-57')")
    db.execute("INSERT OR REPLACE INTO metadata (name, value) VALUES ('name', ?)", (name,))
    db.execute("INSERT OR REPLACE INTO metadata (name, value) VALUES ('description', ?)", (name,))
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Pipeline stage 3: tile-join → merged .mbtiles
# ---------------------------------------------------------------------------

def finalize_tiles(zoom_tiles: List[Tuple[int, Path]], output_path: Path):
    """Merge per-zoom tile files into the final output."""
    tile_files = [path for _, path in sorted(zoom_tiles, key=lambda x: x[0])]
    if len(tile_files) == 1:
        shutil.copy2(tile_files[0], output_path)
        _patch_metadata(output_path, output_path.stem)
    else:
        merge_mbtiles(tile_files, output_path, output_path.stem)


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
# Pipeline orchestration: per-source and by-band
# ---------------------------------------------------------------------------

def process_source(
    source: Source,
    data_dir: Path,
    idx: int,
    native_gdal: bool = True,
    runtime: Optional[str] = None,
    max_workers: int = 1,
) -> List[Tuple[int, Path]]:
    """Run the full pipeline for a single source (standard modes)."""
    label = source.label or f"source{idx}"
    safe_label = re.sub(r'[^\w\-.]', '_', label)

    enc_dir = data_dir / "enc" / safe_label
    geojson_dir = data_dir / "geojson" / safe_label
    tile_dir = data_dir / "tiles"

    enc_dir.mkdir(parents=True, exist_ok=True)
    geojson_dir.mkdir(parents=True, exist_ok=True)

    if source.geojson_dir:
        geojson_dir = source.geojson_dir
        count = len([f for f in geojson_dir.glob("*.geojson") if f.stat().st_size > 100])
        print(f"\n[{label}] Using GeoJSON: {geojson_dir} ({count} layers)")
    else:
        print(f"\n[{label}] z{source.minzoom}-{source.maxzoom}  {source.path}")
        stage_input(source.path, enc_dir, data_dir / "zips")
        enc_files = find_enc_files(enc_dir)
        if not enc_files:
            print(f"ERROR: No .000 files in {source.path}", file=sys.stderr)
            sys.exit(1)
        print(f"Found {len(enc_files)} ENC file(s)")
        export_to_geojson(enc_dir, geojson_dir, enc_files, label=label,
                          native_gdal=native_gdal, runtime=runtime)

    return run_tippecanoe(geojson_dir, tile_dir, f"s{idx}", source.minzoom, source.maxzoom,
                          max_workers=max_workers)


def process_by_band(
    inputs: List[Path],
    data_dir: Path,
    minzoom: int,
    maxzoom: int,
    native_gdal: bool = True,
    runtime: Optional[str] = None,
    max_workers: int = 1,
) -> List[Tuple[int, Path]]:
    """Run the full pipeline with automatic band grouping."""
    print("\n-- By-band mode ---------------------------------------------------")

    zips_dir = data_dir / "zips"
    enc_base = data_dir / "enc"
    geojson_base = data_dir / "geojson"
    tile_dir = data_dir / "tiles"

    # Stage all inputs and find ENC files
    staging_dir = enc_base / "all"
    staging_dir.mkdir(parents=True, exist_ok=True)
    for i, p in enumerate(inputs):
        stage_input(p, staging_dir / f"input{i}", zips_dir)
    all_enc = find_enc_files(staging_dir)
    print(f"Total ENC files found: {len(all_enc)}")

    by_band = group_by_band(all_enc)

    # Warn about non-NOAA files
    if 0 in by_band:
        print(
            f"WARNING: {len(by_band[0])} file(s) don't match NOAA naming (US{{band}}...) "
            f"and will be skipped:",
            file=sys.stderr,
        )
        for f in by_band[0][:5]:
            print(f"  {f.name}", file=sys.stderr)
        if len(by_band[0]) > 5:
            print(f"  ... and {len(by_band[0]) - 5} more", file=sys.stderr)

    # Print band inventory
    print("\nBand inventory:")
    for band in sorted(b for b in by_band if b > 0):
        zoom_min, zoom_max, desc, scale = BAND_ZOOM.get(band, (None, None, "unknown", "?"))
        skipped = ""
        if zoom_min is not None and (zoom_max < minzoom or zoom_min > maxzoom):
            skipped = "  <- outside requested zoom range, skipping"
        print(f"  Band {band} ({desc}, {scale}): {len(by_band[band])} file(s)"
              f"  z{zoom_min}-{zoom_max}{skipped}")

    # Process each band
    all_results: List[Tuple[int, Path]] = []

    for band in sorted(b for b in by_band if b > 0):
        if band not in BAND_ZOOM:
            print(f"\nWARNING: Band {band} not in zoom table, skipping", file=sys.stderr)
            continue

        zoom_min, zoom_max, desc, scale = BAND_ZOOM[band]
        effective_min = max(zoom_min, minzoom)
        effective_max = min(zoom_max, maxzoom)
        if effective_min > effective_max:
            continue

        label = f"band{band}-{desc}"
        print(f"\n-- Band {band}: {desc} ({scale})  z{effective_min}-{effective_max} --")

        band_enc_dir = enc_base / f"band{band}"
        band_geojson_dir = geojson_base / f"band{band}"
        band_enc_dir.mkdir(parents=True, exist_ok=True)
        band_geojson_dir.mkdir(parents=True, exist_ok=True)

        for enc_file in by_band[band]:
            dest = band_enc_dir / enc_file.name
            if not dest.exists():
                shutil.copy2(enc_file, dest)

        export_to_geojson(band_enc_dir, band_geojson_dir, by_band[band], label=label,
                          native_gdal=native_gdal, runtime=runtime)
        results = run_tippecanoe(band_geojson_dir, tile_dir, label, effective_min, effective_max,
                                 max_workers=max_workers)
        all_results.extend(results)

    if not all_results:
        print("ERROR: No tiles produced in by-band mode", file=sys.stderr)
        sys.exit(1)

    return all_results


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_summary(
    zoom_tiles: List[Tuple[int, Path]],
    tiles_path: Path,
    prod_path: Optional[Path],
    by_band: bool,
    input_names: List[str],
    sources: Optional[List[Source]] = None,
):
    size_mb = tiles_path.stat().st_size / 1048576
    if by_band:
        print(f"\nSummary (by-band):")
        print(f"  Inputs: {', '.join(input_names)}")
        for z, path in sorted(zoom_tiles, key=lambda x: x[0]):
            print(f"  z{z}: {path.name} ({path.stat().st_size / 1048576:.1f} MB)")
    elif sources:
        print(f"\nSummary:")
        for i, s in enumerate(sources):
            print(f"  Source {i+1}: {s.label}  z{s.minzoom}-{s.maxzoom}")
    print(f"  Tiles:   {tiles_path} ({size_mb:.1f} MB)")
    if prod_path:
        print(f"  Copied:  {prod_path}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = build_parser()
    args = parser.parse_args()

    data_dir = setup_data_dir()
    out_name = resolve_output_name(args)
    tiles_path = data_dir / "tiles" / out_name

    # Production copy destination
    prod_path = None
    if args.output_dir:
        prod_dir = Path(args.output_dir).resolve()
        prod_dir.mkdir(parents=True, exist_ok=True)
        prod_path = prod_dir / out_name

    if args.by_band:
        if args.geojson_dir:
            parser.error("--geojson-dir cannot be used with --by-band")
        if not args.inputs:
            parser.error("--by-band requires at least one input ZIP or directory")

        input_paths = [Path(p).resolve() for p in args.inputs]
        for p in input_paths:
            if not p.exists():
                print(f"ERROR: {p} not found", file=sys.stderr)
                sys.exit(1)

        native_gdal, runtime = check_deps(need_gdal=True)
        if not native_gdal and runtime:
            pull_image(runtime, GDAL_IMAGE)
        zoom_tiles = process_by_band(input_paths, data_dir, args.minzoom, args.maxzoom,
                                     native_gdal=native_gdal, runtime=runtime,
                                     max_workers=args.jobs)
        finalize_tiles(zoom_tiles, tiles_path)

        if prod_path:
            shutil.copy2(tiles_path, prod_path)
        print_summary(zoom_tiles, tiles_path, prod_path, by_band=True,
                      input_names=[p.name for p in input_paths])
    else:
        sources = build_sources(args, parser)
        validate_sources(sources)

        need_gdal = any(s.geojson_dir is None and s.path is not None for s in sources)
        native_gdal, runtime = check_deps(need_gdal=need_gdal)
        if need_gdal and not native_gdal and runtime:
            pull_image(runtime, GDAL_IMAGE)

        zoom_tiles = []
        for i, source in enumerate(sources):
            zoom_tiles.extend(process_source(source, data_dir, i + 1,
                                             native_gdal=native_gdal, runtime=runtime,
                                             max_workers=args.jobs))
        finalize_tiles(zoom_tiles, tiles_path)

        if prod_path:
            shutil.copy2(tiles_path, prod_path)
        print_summary(zoom_tiles, tiles_path, prod_path, by_band=False,
                      input_names=[], sources=sources)

    print(f"\nAll data preserved in {data_dir.resolve()}/")


if __name__ == "__main__":
    main()
