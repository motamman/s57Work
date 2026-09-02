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

# How many zoom levels past its native ceiling each band renders (capped
# at the global maxzoom). See the comment at the effective_max computation
# in process_by_band for the rationale and cost history.
BAND_ZOOM_EXTENSION = 2

# Per-layer minzoom offset (in zoom levels) from a band's NATIVE minzoom.
# Layers not listed default to 0 (emit from band's bottom zoom).
# Heavy/dense layers get +1 so they only appear at the top of the band.
#
# Enforcement: tippecanoe silently IGNORES a per-layer "minzoom" in the
# -L JSON layer spec (verified against v2.78.0), so these offsets are
# applied by stamping the feature-level `tippecanoe.minzoom` extension
# onto each feature during consolidation (merge_geojson_layer), which
# tippecanoe does honor. Applies in by-band mode and gap fills; plain
# single-source mode has no band context and emits all layers from its
# bottom zoom.
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

# Gap-fill config lives in enc-sources.yaml under the `gap_fills:` key.
# See the comment block in that file for full background on what gap-fill
# is, why it's needed (NOAA legacy ENC discontinuities, addressed by the
# in-progress ENC Rescheming Project), and how to add new gaps.
GAP_FILL_CONFIG_FILE = "enc-sources.yaml"


@dataclass
class GapFillGroup:
    """One group of cells to render at a custom zoom range as a gap fill.
    Loaded from the `gap_fills:` list in enc-sources.yaml."""
    name: str
    zoom_range: Tuple[int, int]
    cells: List[str]
    description: str = ""


def load_gap_fill_config(
    config_path: Optional[Path] = None,
) -> List[GapFillGroup]:
    """Load the list of gap-fill groups from enc-sources.yaml.

    Returns an empty list (gap-fill becomes a no-op) if the file is
    missing, has no `gap_fills:` section, or pyyaml is unavailable.

    Accepts both the current list-of-groups schema (multiple named
    groups, each with its own zoom_range) and the legacy flat schema
    (a single zoom_range + cells dict), to keep older configs working.
    """
    if config_path is None:
        candidates = [
            Path.cwd() / GAP_FILL_CONFIG_FILE,
            Path(__file__).resolve().parent / GAP_FILL_CONFIG_FILE,
        ]
        config_path = next((p for p in candidates if p.exists()), None)
    if config_path is None or not config_path.exists():
        return []
    try:
        import yaml  # type: ignore
    except ImportError:
        print("WARNING: pyyaml not installed; skipping gap-fill config",
              file=sys.stderr)
        return []
    with open(config_path) as f:
        data = yaml.safe_load(f) or {}
    section = data.get("gap_fills")
    if not section:
        return []

    groups: List[GapFillGroup] = []
    if isinstance(section, list):
        # Current schema: list of groups
        for i, g in enumerate(section):
            cells = list(g.get("cells") or [])
            zr = g.get("zoom_range") or [9, 10]
            if not cells:
                continue
            groups.append(GapFillGroup(
                name=g.get("name") or f"gapfill{i}",
                zoom_range=(int(zr[0]), int(zr[1])),
                cells=cells,
                description=g.get("description", ""),
            ))
    elif isinstance(section, dict):
        # Legacy schema: single group inline
        cells = list(section.get("cells") or [])
        zr = section.get("zoom_range") or [9, 10]
        if cells:
            groups.append(GapFillGroup(
                name="gapfill",
                zoom_range=(int(zr[0]), int(zr[1])),
                cells=cells,
            ))
    return groups


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


def _mbtiles_zoom_range(path: Path) -> Optional[Tuple[int, int]]:
    """(minzoom, maxzoom) from an mbtiles' metadata, or None if unreadable.
    Used so a zoom-range change invalidates otherwise-fresh outputs."""
    try:
        db = sqlite3.connect(path)
        meta = dict(db.execute(
            "SELECT name, value FROM metadata "
            "WHERE name IN ('minzoom', 'maxzoom')").fetchall())
        db.close()
        return int(meta["minzoom"]), int(meta["maxzoom"])
    except Exception:
        return None


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
                        output_path: Path,
                        stamp_minzoom: Optional[int] = None):
    """Merge multiple GeoJSON files into one valid FeatureCollection.
    Uses streaming writes to keep memory low.

    When stamp_minzoom is set, each feature gets the tippecanoe feature
    extension {"minzoom": N}, which tippecanoe honors natively — unlike
    per-layer "minzoom" in the -L JSON spec, which it silently ignores."""
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
                if stamp_minzoom is not None and isinstance(feat, dict):
                    ext = feat.get("tippecanoe")
                    ext = dict(ext) if isinstance(ext, dict) else {}
                    ext["minzoom"] = stamp_minzoom
                    feat["tippecanoe"] = ext
                if not first:
                    out.write(",\n")
                json.dump(feat, out)
                first = False
        out.write("\n]}\n")


def _stamp_marker_stale(merged_dir: Path,
                        layer_minzoom: Optional[Dict[str, int]]) -> bool:
    """Freshness guard for per-feature minzoom stamps baked into merged
    GeoJSON: mtime checks can't see stamp-config changes, so the applied
    config is recorded in a marker file. Returns True (treat all merged
    files as stale) when the config differs from what's recorded, and
    updates the marker."""
    merged_dir.mkdir(parents=True, exist_ok=True)
    marker = merged_dir / ".layer-minzoom.json"
    current = json.dumps(layer_minzoom or {}, sort_keys=True)
    try:
        if marker.exists() and marker.read_text() == current:
            return False
    except OSError:
        pass
    marker.write_text(current)
    return True


def consolidate_geojson(geojson_dir: Path, merged_dir: Path,
                        max_workers: int = 1,
                        layer_minzoom: Optional[Dict[str, int]] = None
                        ) -> List[Path]:
    """Group geojson files by layer name and merge into one file per layer.
    Returns list of merged file paths. layer_minzoom maps layer name →
    absolute minzoom to stamp per-feature (see LAYER_MIN_ZOOM_OFFSET)."""
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

    # Per-layer freshness pre-pass (all stale if the stamp config changed)
    force_stale = _stamp_marker_stale(merged_dir, layer_minzoom)
    fresh: List[Path] = []
    stale: List[Tuple[str, List[Path], Path]] = []
    for layer_name, files in layer_groups.items():
        out_path = merged_dir / f"{layer_name}.geojson"
        if not force_stale and output_is_fresh(out_path, files):
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
        stamp = (layer_minzoom or {}).get(layer_name)
        if len(files) == 1 and stamp is None:
            shutil.copy2(files[0], out_path)
        else:
            merge_geojson_layer(layer_name, files, out_path,
                                stamp_minzoom=stamp)
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

    if (output_is_fresh(final, merged_files)
            and _mbtiles_zoom_range(final) == (minzoom, maxzoom)):
        print(f"  [{stem}] z{minzoom}-{maxzoom}: fresh "
              f"({final.stat().st_size / 1048576:.1f} MB), skipping")
        return final

    # Build per-layer JSON layer specs. A per-layer "minzoom" here would
    # be silently ignored by tippecanoe (verified v2.78.0) — zoom gating
    # for heavy layers is instead stamped per-feature during consolidation
    # via the `tippecanoe.minzoom` extension (see LAYER_MIN_ZOOM_OFFSET).
    layer_args = []
    for f in merged_files:
        spec = {"file": str(f), "layer": f.stem}
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
# District-region clipping (by-band mode)
# ---------------------------------------------------------------------------
# BAND_ZOOM_EXTENSION renders low bands into deeper zooms, but band 1/2
# "sailing" cells can span entire ocean basins (US1PO02M covers the whole
# North Pacific), which planted planet-wide z9/z10 tiles and -180..180
# bounds metadata in the Pacific district files (Aug 2026 regression).
# The district's regional extent is defined as the union of its own
# band>=3 (coastal/approach/harbour) chart footprints — never band 1/2 —
# rasterized as a tile mask at REGION_MASK_ZOOM and dilated by one tile
# for margin at the edges. After the per-band tippecanoe runs, tiles in
# band 1/2 files falling outside the mask are deleted and those files'
# bounds metadata recomputed from surviving tiles, so tile-join produces
# a final file whose tile pyramid AND declared bounds are regional. This
# naturally keeps multi-part regions (14CGD = Hawaii + Guam + Samoa)
# because the mask is a tile set, not a single bbox.

REGION_MASK_ZOOM = 11


def _tile_lonlat_bounds(z: int, x: int, tms_y: int
                        ) -> Tuple[float, float, float, float]:
    """(w, s, e, n) lon/lat bounds of an mbtiles tile (TMS row order)."""
    import math
    n = 1 << z
    y = n - 1 - tms_y  # TMS -> XYZ row
    w = x / n * 360.0 - 180.0
    e = (x + 1) / n * 360.0 - 180.0

    def lat(yy: int) -> float:
        t = math.pi - 2.0 * math.pi * yy / n
        return math.degrees(math.atan(math.sinh(t)))

    return w, lat(y + 1), e, lat(y)


def _tile_coords(db: sqlite3.Connection) -> List[Tuple[int, int, int]]:
    """All (zoom, column, row) in an mbtiles, via map table or tiles."""
    has_map = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='map'"
    ).fetchone()
    src = "map" if has_map else "tiles"
    return list(db.execute(
        f"SELECT zoom_level, tile_column, tile_row FROM {src}"))


def _region_mask(band_tiles: Dict[int, Path]) -> set:
    """Union of band>=3 tile coverage as (x, y) pairs at REGION_MASK_ZOOM,
    dilated by one tile. TMS rows throughout — ancestor math (right-shift)
    is row-order-agnostic."""
    mask: set = set()
    for band in sorted(b for b in band_tiles if b >= 3):
        db = sqlite3.connect(str(band_tiles[band]))
        coords = _tile_coords(db)
        db.close()
        if not coords:
            continue
        zmin = min(c[0] for c in coords)
        if zmin < REGION_MASK_ZOOM:
            continue
        shift = zmin - REGION_MASK_ZOOM
        for z, x, y in coords:
            if z == zmin:
                mask.add((x >> shift, y >> shift))
    # Dilate by one tile (~20 km at z11) so band 1/2 coverage doesn't get
    # shaved right at the region's edge.
    n = 1 << REGION_MASK_ZOOM
    dilated = set()
    for x, y in mask:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                dilated.add(((x + dx) % n, min(max(y + dy, 0), n - 1)))
    return dilated


def _recompute_bounds(db: sqlite3.Connection) -> Optional[str]:
    """Bounds string 'w,s,e,n' from the tiles actually present, using the
    deepest zoom level (tightest tile granularity)."""
    coords = _tile_coords(db)
    if not coords:
        return None
    zmax = max(c[0] for c in coords)
    deep = [c for c in coords if c[0] == zmax]
    w = min(_tile_lonlat_bounds(z, x, y)[0] for z, x, y in deep)
    s = min(_tile_lonlat_bounds(z, x, y)[1] for z, x, y in deep)
    e = max(_tile_lonlat_bounds(z, x, y)[2] for z, x, y in deep)
    n = max(_tile_lonlat_bounds(z, x, y)[3] for z, x, y in deep)
    return f"{w},{s},{e},{n}"


def trim_low_bands_to_region(band_tiles: Dict[int, Path]) -> None:
    """Delete band 1/2 tiles outside the district region and fix their
    bounds metadata. No-op when there's nothing to trim against.

    Mutates band_tiles in place: a band left with no tiles at all is
    removed from the dict so the empty file never reaches tile-join."""
    low = sorted(b for b in band_tiles if b < 3)
    if not low:
        return
    if not any(b >= 3 for b in band_tiles):
        print("WARNING: no band>=3 charts to define the district region; "
              "skipping overview-band clipping", file=sys.stderr)
        return

    mask = _region_mask(band_tiles)
    if not mask:
        print("WARNING: empty district-region mask; skipping clipping",
              file=sys.stderr)
        return

    # Ancestor masks for zooms coarser than the mask zoom.
    anc: Dict[int, set] = {}
    for d in range(1, REGION_MASK_ZOOM + 1):
        anc[d] = {(x >> d, y >> d) for x, y in mask}

    for band in low:
        path = band_tiles[band]
        db = sqlite3.connect(str(path))
        doomed = []
        for z, x, y in _tile_coords(db):
            if z >= REGION_MASK_ZOOM:
                s = z - REGION_MASK_ZOOM
                keep = (x >> s, y >> s) in mask
            else:
                keep = (x, y) in anc[REGION_MASK_ZOOM - z]
            if not keep:
                doomed.append((z, x, y))
        if not doomed:
            db.close()
            continue
        has_map = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='map'"
        ).fetchone()
        if has_map:
            db.executemany(
                "DELETE FROM map WHERE zoom_level=? AND tile_column=? "
                "AND tile_row=?", doomed)
            db.execute("DELETE FROM images WHERE tile_id NOT IN "
                       "(SELECT DISTINCT tile_id FROM map)")
        else:
            db.executemany(
                "DELETE FROM tiles WHERE zoom_level=? AND tile_column=? "
                "AND tile_row=?", doomed)
        bounds = _recompute_bounds(db)
        if bounds:
            db.execute("INSERT OR REPLACE INTO metadata (name, value) "
                       "VALUES ('bounds', ?)", (bounds,))
            w, s_, e, n = (float(v) for v in bounds.split(","))
            db.execute("INSERT OR REPLACE INTO metadata (name, value) "
                       "VALUES ('center', ?)",
                       (f"{(w + e) / 2},{(s_ + n) / 2},{REGION_MASK_ZOOM}",))
        else:
            # Nothing survived: drop the stale (planet-wide) bounds rather
            # than leave them describing tiles that no longer exist.
            db.execute("DELETE FROM metadata WHERE name IN "
                       "('bounds', 'center')")
        db.commit()
        db.execute("VACUUM")
        db.close()
        if bounds:
            print(f"  [band{band}] clipped {len(doomed)} out-of-region "
                  f"tile(s), bounds -> {bounds}")
        else:
            print(f"  [band{band}] clipped all {len(doomed)} tile(s) as "
                  f"out-of-region; excluding empty band from merge")
            del band_tiles[band]


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
# Gap fill: render high-band detail cells at lower zooms to cover
#           documented NOAA legacy-ENC coverage holes.
# ---------------------------------------------------------------------------
#
# Why this exists
# ---------------
# NOAA's legacy ENCs were compiled cell-by-cell from paper-chart sources
# at fixed scales over many years. The result is genuine discontinuities
# in the lower-band overview cells: certain coastal stretches have band
# 3+ detail cells but no band 2 (z=9-10) cell at all, or have band 2 and
# band 3 catalog cells whose actual polygons stop short of the coast,
# leaving holes at z=9-12.
#
# NOAA acknowledges this and is rebuilding the ENC catalog as a continuous
# gridded dataset under the multi-year ENC Rescheming Project, working
# from band 5 outward (band 5 first, then 4, 3, 2, 1). Until band 2 is
# fully reschemed, this script's "gap fill" pass synthesizes the missing
# overview/general/coastal coverage by rendering the band 3-5 detail
# cells at lower zooms.
#
# How it works
# ------------
# Each `gap_fills:` group in enc-sources.yaml lists a set of higher-band
# cell IDs and a zoom_range. process_gap_fill():
#   1. Globs the existing per-band GeoJSON output (data/geojson/band*/)
#      for files matching each cell ID. Cells not present in this build's
#      input are silently skipped.
#   2. Consolidates the matching per-cell GeoJSON into a per-group merged
#      directory, grouped by layer name.
#   3. Runs one tippecanoe pass per group at its configured zoom_range,
#      producing data/tiles/gapfill-<group>.mbtiles.
#
# The resulting mbtiles paths are inserted into the final tile-join order
# between band 2 and band 3 (see process_by_band). Since `tile-join` lets
# later inputs win on overlap, original-band detail still wins everywhere
# it has data — fills only show up in tiles the original bands left empty.
#
# References (full context in the gap_fills: comment block of
# enc-sources.yaml):
#   • Rescheming program:
#       https://nauticalcharts.noaa.gov/charts/rescheming-and-improving-electronic-navigational-charts.html
#   • Cell creation status map:
#       https://nauticalcharts.noaa.gov/updates/follow-the-status-of-electronic-navigational-chart-improvements-with-noaas-new-map-viewer/

def process_gap_fill(
    data_dir: Path,
    minzoom: int,
    maxzoom: int,
    max_workers: int,
) -> List[Path]:
    """Run one tippecanoe pass per configured gap-fill group.

    Returns a list of mbtiles paths produced (empty list if nothing to do
    or no cells matched in this build). The caller is responsible for
    inserting these into the tile-join order at the right spot — see
    process_by_band().
    """
    groups = load_gap_fill_config()
    if not groups:
        return []

    geojson_root = data_dir / "geojson"
    if not geojson_root.is_dir():
        return []

    out_paths: List[Path] = []
    for group in groups:
        out = _process_gap_fill_group(
            group, data_dir, geojson_root, minzoom, maxzoom, max_workers)
        if out is not None:
            out_paths.append(out)
    return out_paths


def _process_gap_fill_group(
    group: GapFillGroup,
    data_dir: Path,
    geojson_root: Path,
    minzoom: int,
    maxzoom: int,
    max_workers: int,
) -> Optional[Path]:
    """Render one gap-fill group's cells at its configured zoom range."""
    effective_min = max(group.zoom_range[0], minzoom)
    effective_max = min(group.zoom_range[1], maxzoom)
    if effective_min > effective_max:
        return None

    # Cells live in whichever band's geojson dir matches their ID prefix.
    # We just glob across all band* dirs to find each requested cell —
    # cells absent from this build's ENC input produce no matches and
    # are silently skipped.
    cell_files: Dict[str, List[Path]] = {}
    for cell_id in group.cells:
        matches: List[Path] = []
        for band_dir in sorted(geojson_root.glob("band*")):
            if band_dir.is_dir():
                matches.extend(band_dir.glob(f"*_{cell_id}.geojson"))
        if matches:
            cell_files[cell_id] = matches

    if not cell_files:
        return None

    present = sorted(cell_files)
    print(f"\n-- Gap fill [{group.name}]: {len(present)} cell(s) at "
          f"z{effective_min}-{effective_max} --")
    print(f"   Cells: {', '.join(present)}")

    # Group per-cell GeoJSON files by layer name. The naming convention
    # comes from process_band's GDAL export step: "LAYER_CELLSTEM.geojson".
    layer_groups: Dict[str, List[Path]] = {}
    for files in cell_files.values():
        for f in files:
            if f.stat().st_size <= 100:
                continue
            layer_name = f.stem.split("_")[0]
            layer_groups.setdefault(layer_name, []).append(f)

    if not layer_groups:
        return None

    safe_name = re.sub(r'[^\w\-.]', '_', group.name)
    merged_dir = data_dir / "merged" / f"gapfill-{safe_name}"
    merged_dir.mkdir(parents=True, exist_ok=True)

    # Heavy-layer minzoom stamps, offset from the group's configured
    # bottom zoom (config-static, so cached merges stay valid).
    layer_minzoom = {name: group.zoom_range[0] + off
                     for name, off in LAYER_MIN_ZOOM_OFFSET.items() if off}
    force_stale = _stamp_marker_stale(merged_dir, layer_minzoom)

    for layer_name, files in layer_groups.items():
        out_path = merged_dir / f"{layer_name}.geojson"
        if not force_stale and output_is_fresh(out_path, files):
            continue
        stamp = layer_minzoom.get(layer_name)
        if len(files) == 1 and stamp is None:
            shutil.copy2(files[0], out_path)
        else:
            merge_geojson_layer(layer_name, files, out_path,
                                stamp_minzoom=stamp)

    tile_dir = data_dir / "tiles"
    return run_tippecanoe_for_source(
        merged_dir, tile_dir, f"gapfill-{safe_name}",
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

    # Stage 3: Consolidate. Heavy layers get a per-feature minzoom stamp,
    # offset from the band's NATIVE minzoom (not the CLI-effective one) so
    # cached merged files stay valid across zoom-argument changes.
    band_zoom_min = BAND_ZOOM[band][0]
    layer_minzoom = {name: band_zoom_min + off
                     for name, off in LAYER_MIN_ZOOM_OFFSET.items() if off}
    consolidate_geojson(band_geojson_dir, band_merged_dir,
                        max_workers=max_workers,
                        layer_minzoom=layer_minzoom)

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
        if zoom_min is not None and zoom_min > maxzoom:
            skipped = "  <- starts above max zoom, skipping"
        render_max = (min(zoom_max + BAND_ZOOM_EXTENSION, maxzoom)
                      if zoom_max is not None else maxzoom)
        print(f"  Band {band} ({desc}, {scale}): {len(by_band[band])} file(s)"
              f"  z{zoom_min}-{zoom_max} (renders to z{render_max}){skipped}")

    # Build list of bands to process
    band_tasks = []
    for band in sorted(b for b in by_band if b > 0):
        if band not in BAND_ZOOM:
            continue
        zoom_min, zoom_max, desc, scale = BAND_ZOOM[band]
        effective_min = max(zoom_min, minzoom)
        # Each band renders BAND_ZOOM_EXTENSION levels past its native
        # ceiling (capped at the global maxzoom): wherever no finer band
        # exists, its tiles are the best available chart at deeper zooms,
        # and tile-join (coarse→fine order) lets finer bands win on
        # overlap. Band 4 (native z14) thus reaches z16, closing the
        # no-harbour-chart blanks along the whole charted coast. The cap
        # exists because extending without limit is intractable: overview
        # bands cover entire ocean basins, and rendering them to z16
        # multiplied district builds 4-6x in size and blew the 6h CI
        # timeout / runner disk on Pacific districts (band 1 of 14CGD at
        # z16 = tiling the whole Pacific EEZ at harbour zoom).
        effective_max = min(zoom_max + BAND_ZOOM_EXTENSION, maxzoom)
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

    # Clip band 1/2 output to the district's regional extent (union of
    # its band>=3 footprints) — ocean-basin overview cells otherwise put
    # planet-wide tiles and bounds into every district file.
    trim_low_bands_to_region(band_tiles)
    if not band_tiles:
        print("ERROR: No tiles left after district-region clipping",
              file=sys.stderr)
        sys.exit(1)

    # Gap fill: one mbtiles per configured group. See process_gap_fill()
    # and the `gap_fills:` comment block in enc-sources.yaml for the full
    # rationale (NOAA legacy-ENC discontinuities being closed by the ENC
    # Rescheming Project).
    gap_tile_paths = process_gap_fill(data_dir, minzoom, maxzoom, max_workers)

    # Coarse → fine ordering by band number (band 1 = overview, band 6 = berthing)
    sorted_bands = sorted(band_tiles)
    ordered = [band_tiles[b] for b in sorted_bands]
    if gap_tile_paths:
        # Slot all fills between band 2 and band 3. tile-join lets later
        # inputs win on overlap, so this position lets the original band
        # 3+ outputs win wherever they have data — fills only appear in
        # tiles the original bands left empty.
        insert_idx = next((i for i, b in enumerate(sorted_bands) if b > 2),
                          len(ordered))
        for p in gap_tile_paths:
            ordered.insert(insert_idx, p)
            insert_idx += 1
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
