# s57-to-mbtiles.py — Detailed Usage Guide

Convert NOAA S-57 ENC charts (`.000` files) into vector MBTiles for use with SignalK / Freeboard-SK.

---

## Requirements

| Tool | Purpose | Install |
|------|---------|---------|
| **GDAL** (`ogr2ogr`, `ogrinfo`) | Converts S-57 layers to GeoJSON. Native install is used when found; otherwise the script runs the `ghcr.io/osgeo/gdal:alpine-small-latest` container via **podman** or **docker** | System package manager, or podman/docker |
| **tippecanoe** | Converts GeoJSON into vector `.mbtiles` tiles | Build from source or package manager |
| **tile-join** | Merges multiple `.mbtiles` into one (ships with tippecanoe) | Included with tippecanoe |
| **Python 3** | Runs the script itself | Pre-installed on most systems |

No Python dependencies beyond the standard library. `pyyaml` is optional: without it the gap-fill config in `enc-sources.yaml` is skipped with a warning.

See [INSTALL.md](INSTALL.md) for platform-specific setup.

---

## Input: Where to Get ENC Files

Download NOAA ENC charts from:
- NOAA ENC Direct to GIS: individual chart ZIPs
- NOAA Office of Coast Survey: bulk region downloads

Each download is a ZIP containing one or more `.000` files. You can pass ZIPs directly to the script, or extract them and pass the directory.

### NOAA ENC Filename Convention

```
US{band}{region_code}{number}{edition}.000
```

- **Band** (digit 1–6): the chart's usage band / scale level
- **Region code** (2–3 letters): geographic area (e.g., `MA1`, `CT1`, `NY1`, `BOS`, `PVD`, `EC`)
- These are **not** always state abbreviations — `BOS` = Boston area, `PVD` = Providence area, `EC` = East Coast regional, `FAV` = Fall River/New Bedford area

Cells may ship with `.001`, `.002`, … update files alongside the `.000` base. The script copies those along with the base so GDAL applies them.

---

## Pipeline Overview

For each band or source, the script runs these stages in order:

```
1. Extract    ZIPs / copy directories        → data/enc/
2. Find       every .000 ENC file recursively
3. GDAL       ogr2ogr → one GeoJSON per S-57 layer per cell → data/geojson/
3b. Resolve   (by-band) clip legacy cells under reschemed cells of the same band → data/geojson/<band>.resolved/
4. Consolidate merge per-cell GeoJSON into one file per layer → data/merged/
5. tippecanoe one run per band/source over its full zoom range → data/tiles/
6. tile-join  merge all band/source .mbtiles into the final file
```

Every stage checks freshness by file modification time and skips work whose outputs are already newer than their inputs, so re-runs after a partial failure or an added input only redo what changed.

### Skipped S-57 Layers

These metadata-only layers are automatically skipped during GDAL export:
`DSID`, `C_AGGR`, `C_ASSO`, `Generic`

### Sounding Depths (SOUNDG)

The SOUNDG layer gets special handling. S-57 stores soundings as MultiPointZ geometry where the depth is the Z coordinate. The script uses two GDAL S-57 driver options to make depths usable:

- `SPLIT_MULTIPOINT=YES` — explodes each MultiPoint into individual Point features (one per sounding)
- `ADD_SOUNDG_DEPTH=YES` — adds a `DEPTH` property to each feature with the sounding value

This means depth readings show up as a `DEPTH` attribute in the vector tiles, which renderers can display as labels.

### Heavy Layers Start One Zoom Later

In by-band mode and gap fills, dense layers (`SOUNDG`, lights, buoys, beacons, obstructions, wrecks, rocks) are held back one zoom level from their band's bottom zoom, so a band's overview zoom is not swamped with point features. The offsets live in `LAYER_MIN_ZOOM_OFFSET` at the top of the script and are stamped per feature during consolidation. Single-source mode has no band context and emits every layer from its bottom zoom.

---

## Modes of Operation

### 1. Single Source (simplest)

Process one ZIP or directory with a flat zoom range.

```bash
python3 s57-to-mbtiles.py MA_ENCs.zip
python3 s57-to-mbtiles.py MA_ENCs.zip -o ma-charts.mbtiles --minzoom 9 --maxzoom 16
python3 s57-to-mbtiles.py ./extracted_encs/
```

- Default zoom range: z9–z16
- Output filename defaults to `{input_stem}.mbtiles`

### 2. Two-Source Split

Merge two sources at different zoom ranges, split at a specific zoom level.

```bash
python3 s57-to-mbtiles.py region03.zip RI_detail.zip --split 12 -o ri-merged.mbtiles
```

- `--split 12` means: first input gets z9–z11, second input gets z12–z16
- Useful when you have a coarse regional chart and a detailed local chart

### 3. Multi-Source Explicit Ranges

Specify exact zoom ranges per source using the `--sources` flag.

```bash
python3 s57-to-mbtiles.py --sources overview.zip:7-9 regional.zip:10-12 detail.zip:13-16 -o merged.mbtiles
```

- Format: `path:minzoom-maxzoom`
- The script warns if zoom ranges overlap: tile-join merges same-layer features from both sources into the same tiles, it does not let the later source win

### 4. By-Band (recommended for multi-state regions)

**This is the mode you almost certainly want for combining multiple states.**

```bash
python3 s57-to-mbtiles.py CT_ENCs.zip RI_ENCs.zip MA_ENCs.zip NY_ENCs.zip --by-band -o ct-ri-ma-ny-layers.mbtiles
```

How it works:

1. Extracts all ZIPs / copies all directories into a staging area (`data/enc/all/input0`, `input1`, etc.)
2. Finds every `.000` file recursively
3. Groups files by NOAA usage band (the first digit after `US` in the filename)
4. Runs GDAL → consolidate → tippecanoe separately for each band, bands in parallel. Each band starts at its native minzoom and renders **two zoom levels past its native ceiling**, capped at `--maxzoom`, so wherever no finer chart exists the best available band still fills the deeper zooms:

| Band | Type | Scale | Native zoom | Renders to |
|------|------|-------|-------------|------------|
| 1 | Overview | ~1:3,500,000 | z7–z8 | z10 |
| 2 | General | ~1:700,000 | z9–z10 | z12 |
| 3 | Coastal | ~1:90,000 | z11–z12 | z14 |
| 4 | Approach | ~1:22,000 | z13–z14 | z16 |
| 5 | Harbour | ~1:8,000 | z15–z16 | z16 (default `--maxzoom`) |
| 6 | Berthing | ~1:3,000 | z17–z18 | needs `--maxzoom 17` or higher |

   **Finer chart wins where bands overlap.** tile-join does not do this for you: when two inputs have the same tile and layer it merges their features into one layer, so the pipeline enforces it before tiling. At every zoom where a band shares zooms with a finer band (band 2's z11–12 with band 3, band 3's z13–14 with band 4, band 4's z15–16 with band 5), the union of the finer band's chart footprints (`M_COVR`, `CATCOV=1`) is erased from the coarser band's features with `ogr2ogr -clipsrc`, into `data/merged/<band>.minus-<finer>/`. Those zooms are tiled from the erased copy; the band's native zooms are tiled from the untouched merged layers. A coarse chart therefore appears only where no finer chart has coverage, exactly as an ECDIS displays it, and the seams follow chart boundaries rather than tile edges. This step needs GDAL with SpatiaLite/GEOS spatial SQL (Ubuntu `gdal-bin`, Homebrew `gdal`).
   **Same-band overlaps are resolved before consolidation.** NOAA's rescheming currently ships legacy and reschemed cells of one band that overlap with data in both (see `docs/SAME-BAND-OVERLAP.md`). Within a band the reschemed cell wins: every layer of the legacy cell is clipped under the reschemed cells' `M_COVR` footprints into `data/geojson/<band>.resolved/` and those copies replace the originals at consolidation. Cells are classified by NOAA's naming convention, which is unambiguous for bands 1-2 only; overlaps in bands 3-6 are reported as warnings and left alone. Every pair is recorded in `data/merged/<band>/.same-band-overlaps.json`.
5. Clips band 1/2 output to the district's region. Overview cells can span an entire ocean basin (`US1PO02M` covers the whole North Pacific), which would otherwise put planet-wide tiles and −180..180 bounds into every district file. The region is the union of the district's own band 3+ chart footprints as a z11 tile mask, dilated by one tile. The clipped result is written to a separate `bandN-*.region.mbtiles`; the band's tippecanoe output is never modified, so resume stays correct if a later run adds inputs that widen the region.
6. Builds any gap fills configured in `enc-sources.yaml` (see below).
7. Merges everything with tile-join. Gap fills go through the same erase rule with priority `cellband − 0.1`. At each zoom the band NOAA compiled for that zoom wins outright wherever it has coverage; elsewhere finer data wins. A fill therefore shows only in the holes, and a fill of band N cells is fully erased wherever band N itself renders, so nothing is tiled twice.

**Why by-band is better than per-state:** State boundaries are irrelevant to chart scale. A CT approach chart and a RI approach chart are the same band and belong at the same zoom level. By-band groups them correctly so no source overwrites another.

You can clip the zoom range with `--minzoom` and `--maxzoom`:

```bash
python3 s57-to-mbtiles.py CT.zip RI.zip MA.zip NY.zip --by-band --minzoom 9 --maxzoom 16 -o region.mbtiles
```

This skips bands whose zoom range falls entirely outside the requested range, and clips bands that partially overlap.

Files that don't match the NOAA `US{digit}...` naming convention are reported as warnings and skipped.

#### Gap fills

NOAA's legacy ENC catalog has holes where a stretch of coast has band 3+ detail cells but no band 2 cell, leaving blanks at z9–z12. The `gap_fills:` section of `enc-sources.yaml` lists named groups of higher-band cells to render at a lower zoom range to cover those holes. Cells that are not part of the current build are skipped silently. Each group produces `data/tiles/gapfill-<name>.mbtiles`. The comment block in `enc-sources.yaml` explains the background and how to add a gap.

### 5. Skip GDAL (reuse existing GeoJSON)

If you already have GeoJSON files (from a previous run's `data/geojson/`, for example):

```bash
python3 s57-to-mbtiles.py --geojson-dir ./data/geojson/band3/ --minzoom 11 --maxzoom 12 -o band3.mbtiles
```

- Skips GDAL entirely — goes straight to consolidate and tippecanoe
- Cannot be combined with `--by-band`
- The GeoJSON directory must contain `.geojson` files; files under 100 bytes are ignored

In practice the freshness checks make this mode rarely necessary: re-running the same by-band command skips GDAL for every cell whose GeoJSON is already up to date.

---

## All Command-Line Options

| Option | Description | Default |
|--------|-------------|---------|
| `inputs` | Positional: ZIP file(s) or directory(ies) with .000 ENC files | — |
| `--by-band` | Auto-group by NOAA band and assign zoom ranges | off |
| `--sources FILE:MIN-MAX` | Explicit per-source zoom ranges | — |
| `--split ZOOM` | Zoom split point for two-input mode | — |
| `-o, --output` | Output `.mbtiles` filename | `{input_stem}.mbtiles` (`-merged` appended for multiple inputs) |
| `--output-dir` | Production directory to copy the final `.mbtiles` into. The file always stays in `data/tiles/` as well | no copy |
| `--minzoom` | Minimum zoom level | 9 |
| `--maxzoom` | Maximum zoom level | 16 |
| `--geojson-dir` | Use existing GeoJSON, skip GDAL | — |
| `-j, --jobs` | Parallel workers for GDAL export, consolidation, and bands | half the CPU count |

---

## Data Directory

Everything the script produces lives in `./data/` under the current working directory, and **nothing is deleted between runs**. This is intentional — the GDAL export is the slowest step, and keeping its output lets a re-run skip straight to whatever actually changed.

Layout for `--by-band` mode:

```
data/
├── zips/                       # copy of every input ZIP
├── enc/
│   ├── all/input0/, input1/…   # staging: each input extracted here
│   └── band3/, band4/, …       # .000 (+ .001… updates) copied per band
├── geojson/
│   ├── band3/, band4/, …       # LAYER_CELL.geojson from ogr2ogr
│   └── band2.resolved/         # legacy cells clipped under reschemed cells (Stage 3b);
│                               # mask/<CELL>/clip.geojson = the erase polygon
├── merged/
│   ├── band3/, band4/, …       # one LAYER.geojson per band
│   ├── band3-coastal_z13-14.minus-band4-approach/
│   │                           # band 3 with band 4 footprints erased
│   │                           # (mask/clip.geojson = erase polygon)
│   └── gapfill-<name>/         # per gap-fill group
└── tiles/
    ├── band3-coastal.mbtiles   # native zooms (z11-12)
    ├── band3-coastal_z13-14.minus-band4-approach.mbtiles  # extended zooms
    ├── band2-general.region.mbtiles  # band 1/2 clipped copies fed to tile-join
    ├── gapfill-<name>*.mbtiles
    └── <output name>.mbtiles   # final merged file
```

For standard (non-by-band) mode the per-source directories are named after the input file (`data/enc/NY_ENCs.zip/`, `data/geojson/NY_ENCs.zip/`, …) and the tippecanoe outputs are `data/tiles/s1.mbtiles`, `s2.mbtiles`, ….

To force a full rebuild, delete `data/` (or just `data/tiles/` to redo only the tippecanoe stage).

### What gets skipped on a re-run

- **GDAL**: a cell is re-exported only if its `.000` or any update file is newer than its existing GeoJSON.
- **Same-band overlap resolution**: detection is cached on the cells' `M_COVR` exports and reruns only when one changes; a clipped legacy layer is redone only if its source file or the clip polygon is newer. Bands with no resolvable pair cost nothing.
- **Consolidate**: a merged layer is rebuilt only if any of its per-cell inputs (or its clipped replacement) is newer, or the heavy-layer minzoom config changed.
- **Erase**: the erase polygon is recomputed every run (cheap) but only rewritten when it changes; an erased layer is re-clipped only if its source layer or the erase polygon is newer.
- **tippecanoe**: a tileset's `.mbtiles` is reused if it is newer than every merged (or erased) layer and its stored zoom range matches the requested one.
- **Region clipping, gap-fill merging, tile-join**: cheap, always recomputed.

---

## Tippecanoe Behavior

- Runs **once per band or source** over its full zoom range (`-Z min -z max`), with every merged layer passed as a separate `-L` layer
- Uses `--no-tile-size-limit`, `--no-feature-limit`, `--no-simplification`, and `--no-tiny-polygon-reduction` (nautical charts need every feature at full fidelity), plus `--detect-shared-borders` and `--buffer=80`
- GeoJSON filenames like `DEPARE_US5MA1SK.geojson` get the layer name by removing the trailing cell name (so the tile layer is `DEPARE`, and `M_COVR_US5MA1SK.geojson` becomes `M_COVR`)

---

## Output

The final `.mbtiles` file is a standard MBTiles v1.3 vector tileset, written to `data/tiles/<name>.mbtiles` and copied to `--output-dir` if given. Metadata is patched to set `type=S-57` and `name`/`description` to the output stem. In by-band mode the declared bounds reflect the district region, not the full extent of its overview cells.

### Using with SignalK

Copy or symlink the output to your SignalK charts directory:

```bash
cp ct-ri-ma-ny-layers.mbtiles ~/.signalk/charts-simple/
```

Or use the SignalK charts plugin upload feature.

The chart should appear in Freeboard-SK as a vector overlay.

---

## Long-Running Jobs

The GDAL export and tippecanoe steps can take a long time, especially for multi-state by-band runs. Run with `nohup` and a log file:

```bash
nohup python3 s57-to-mbtiles.py CT.zip RI.zip MA.zip NY.zip \
  --by-band -o ct-ri-ma-ny-layers.mbtiles \
  --output-dir ~/.signalk/charts-simple/ \
  > ~/s57-rebuild.log 2>&1 &
```

Monitor with:

```bash
tail -f ~/s57-rebuild.log
```

If a run dies part-way, re-run the same command: the freshness checks pick up where it left off.

---

## Common S-57 Layers in Output Tiles

| Layer | Description |
|-------|-------------|
| SOUNDG | Depth soundings (individual points with `DEPTH` property in meters) |
| DEPARE | Depth areas (polygons with `DRVAL1`/`DRVAL2` depth range) |
| DEPCNT | Depth contour lines |
| LNDARE | Land areas |
| COALNE | Coastline |
| BOYCAR | Cardinal buoys |
| BOYLAT | Lateral buoys |
| BCNSPP | Special purpose beacons |
| LIGHTS | Lights |
| NAVLNE | Navigation lines |
| OBSTRN | Obstructions |
| WRECKS | Wrecks |
| BRIDGE | Bridges |
| RESARE | Restricted areas |
| ACHARE | Anchorage areas |
| TSSLPT | Traffic separation scheme lanes |
| RIVERS | Rivers |
| LAKARE | Lake areas |
| SLCONS | Shoreline constructions (piers, wharves) |

Many more layers exist depending on chart content. The script exports all layers except the four skipped metadata layers.

---

## Example: Full Four-State Build

```bash
# Download CT, RI, MA, NY ENC ZIPs from NOAA

# Run the conversion (by-band mode, recommended)
nohup python3 s57-to-mbtiles.py \
  CT_ENCs.zip RI_ENCs.zip MA_ENCs.zip NY_ENCs.zip \
  --by-band \
  -o ct-ri-ma-ny-layers.mbtiles \
  --output-dir ~/.signalk/charts-simple/ \
  > ~/s57-build.log 2>&1 &

# Watch progress
tail -f ~/s57-build.log

# When done, restart SignalK or refresh Freeboard-SK to see the new chart
```
