# s57-to-mbtiles.py — Detailed Usage Guide

Convert NOAA S-57 ENC charts (`.000` files) into vector MBTiles for use with SignalK / Freeboard-SK.

---

## Requirements

| Tool | Purpose | Install |
|------|---------|---------|
| **podman** or **docker** | Runs the GDAL container (`ghcr.io/osgeo/gdal:alpine-small-latest`) for ogr2ogr conversions | System package manager |
| **tippecanoe** | Converts GeoJSON into vector `.mbtiles` tiles | Build from source or package manager |
| **tile-join** | Merges multiple `.mbtiles` into one (ships with tippecanoe) | Included with tippecanoe |
| **Python 3** | Runs the script itself | Pre-installed on most systems |

No Python dependencies beyond the standard library.

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

---

## Pipeline Overview

For each band or source, the script runs these steps in order:

```
1. Extract ZIPs (if input is a ZIP file)
2. Find all .000 ENC files recursively
3. ogr2ogr (GDAL, via container) → one GeoJSON file per S-57 layer per ENC file
4. tippecanoe → one .mbtiles per zoom level
5. tile-join → merge all zoom-level tiles into one final .mbtiles
```

### Skipped S-57 Layers

These metadata-only layers are automatically skipped during GDAL export:
`DSID`, `C_AGGR`, `C_ASSO`, `Generic`

### Sounding Depths (SOUNDG)

The SOUNDG layer gets special handling. S-57 stores soundings as MultiPointZ geometry where the depth is the Z coordinate. The script uses two GDAL S-57 driver options to make depths usable:

- `SPLIT_MULTIPOINT=YES` — explodes each MultiPoint into individual Point features (one per sounding)
- `ADD_SOUNDG_DEPTH=YES` — adds a `DEPTH` property to each feature with the sounding value

This means depth readings show up as a `DEPTH` attribute in the vector tiles, which renderers can display as labels.

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
- The script warns if zoom ranges overlap (later source wins in tile-join)

### 4. By-Band (recommended for multi-state regions)

**This is the mode you almost certainly want for combining multiple states.**

```bash
python3 s57-to-mbtiles.py CT_ENCs.zip RI_ENCs.zip MA_ENCs.zip NY_ENCs.zip --by-band -o ct-ri-ma-ny-layers.mbtiles
```

How it works:

1. Extracts all ZIPs / copies all directories into a staging area (`all_enc/input0`, `input1`, etc.)
2. Finds every `.000` file recursively
3. Groups files by NOAA usage band (extracted from the first digit after `US` in the filename)
4. Runs the full GDAL → tippecanoe pipeline separately for each band, using the correct zoom range:

| Band | Type | Scale | Zoom Range |
|------|------|-------|------------|
| 1 | Overview | ~1:3,500,000 | z7–z8 |
| 2 | General | ~1:700,000 | z9–z10 |
| 3 | Coastal | ~1:90,000 | z11–z12 |
| 4 | Approach | ~1:22,000 | z13–z14 |
| 5 | Harbour | ~1:8,000 | z15–z16 |
| 6 | Berthing | ~1:3,000 | z17–z18 |

5. Merges all band tiles with tile-join (coarse bands first, detail wins on overlap)

**Why by-band is better than per-state:** State boundaries are irrelevant to chart scale. A CT approach chart and a RI approach chart are the same band and belong at the same zoom level. By-band groups them correctly so no source overwrites another.

You can clip the zoom range with `--minzoom` and `--maxzoom`:

```bash
python3 s57-to-mbtiles.py CT.zip RI.zip MA.zip NY.zip --by-band --minzoom 9 --maxzoom 16 -o region.mbtiles
```

This skips bands whose zoom range falls entirely outside the requested range, and clips bands that partially overlap.

Files that don't match the NOAA `US{digit}...` naming convention are reported as warnings and skipped.

### 5. Skip GDAL (reuse existing GeoJSON)

If you already have GeoJSON files (from a previous run's temp directory, for example):

```bash
python3 s57-to-mbtiles.py --geojson-dir /tmp/s57_xxxx/geojson/ --minzoom 9 --maxzoom 16 -o charts.mbtiles
```

- Skips the GDAL container entirely — goes straight to tippecanoe
- Cannot be combined with `--by-band`
- The GeoJSON directory must contain `.geojson` files; files under 100 bytes are ignored

---

## All Command-Line Options

| Option | Description | Default |
|--------|-------------|---------|
| `inputs` | Positional: ZIP file(s) or directory(ies) with .000 ENC files | — |
| `--by-band` | Auto-group by NOAA band and assign zoom ranges | off |
| `--sources FILE:MIN-MAX` | Explicit per-source zoom ranges | — |
| `--split ZOOM` | Zoom split point for two-input mode | — |
| `-o, --output` | Output `.mbtiles` filename | auto-generated |
| `--output-dir` | Directory for output file | `.` (current dir) |
| `--minzoom` | Minimum zoom level | 9 |
| `--maxzoom` | Maximum zoom level | 16 |
| `--geojson-dir` | Use existing GeoJSON, skip GDAL | — |
| `--delete-temp` | Remove temp files after completion | off (keeps them) |

---

## Temp Files

By default, the script creates a temp directory at `/tmp/s57_XXXXXXXX/` and **does not delete it**. This is intentional — the GDAL export is the slowest step, and keeping the GeoJSON lets you re-run tippecanoe without redoing GDAL.

The temp directory structure for `--by-band` mode:

```
/tmp/s57_XXXXXXXX/
├── all_enc/           # Staging area for all input ENC files
│   ├── input0/        # First ZIP/directory extracted here
│   ├── input1/        # Second ZIP/directory
│   ├── input2/        # etc.
│   └── input3/
├── band2/
│   ├── enc/           # .000 files for this band (copied from all_enc)
│   ├── geojson/       # GeoJSON output from ogr2ogr
│   └── tiles/         # .mbtiles per zoom level from tippecanoe
├── band3/
│   ├── enc/
│   ├── geojson/
│   └── tiles/
├── band4/
│   └── ...
└── band5/
    └── ...
```

For standard (non-by-band) mode:

```
/tmp/s57_XXXXXXXX/
├── source1/
│   ├── enc/
│   ├── geojson/
│   └── tiles/
└── source2/
    └── ...
```

Use `--delete-temp` to clean up automatically. Or pass the temp directory path to `--geojson-dir` in a later run to skip GDAL.

---

## Tippecanoe Behavior

- Runs **one zoom level at a time** (single-zoom `-z Z -Z Z` invocations)
- Uses `--no-tile-size-limit` and `--no-feature-limit` (nautical charts need all features)
- If a per-zoom `.mbtiles` already exists in the temp tiles directory and is non-empty, it **skips** that zoom level (resume-friendly)
- GeoJSON filenames like `DEPARE_US5MA1SK.geojson` get the layer name from the part before the first `_` (so the tile layer is `DEPARE`)

---

## Output

The final `.mbtiles` file is a standard MBTiles v1.3 vector tileset. Metadata is patched to set `type=S-57`.

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
| BCNSPC | Special purpose beacons |
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
