# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Single-script Python tool (`s57-to-mbtiles.py`) that converts NOAA S-57 ENC nautical charts (`.000` files) into vector MBTiles for use with SignalK / Freeboard-SK.

There are no tests or linting configured for this project.

## External Dependencies

- **GDAL** (`ogr2ogr`, `ogrinfo`) — native install or via container (`ghcr.io/osgeo/gdal:alpine-small-latest`) using podman/docker
- **tippecanoe + tile-join** — converts GeoJSON to vector `.mbtiles` tiles (native install)
- **Python 3** — standard library only

## Running

```bash
# Single source
./s57-to-mbtiles.py NY_ENCs.zip

# By-band mode (recommended for multi-state)
./s57-to-mbtiles.py CT_ENCs.zip RI_ENCs.zip MA_ENCs.zip NY_ENCs.zip --by-band -o merged.mbtiles

# Reuse existing GeoJSON (skip slow GDAL step)
./s57-to-mbtiles.py --geojson-dir ./data/geojson/band3/ --minzoom 11 --maxzoom 12
```

## Pipeline Architecture

Single-file pipeline with five stages per band/source:

1. **Extract** — unzip inputs into `data/enc/`
2. **Find** — discover `.000` ENC files (group by NOAA band in `--by-band` mode)
3. **GDAL export** — `ogr2ogr` (native or container) converts each ENC layer to GeoJSON in `data/geojson/`. SOUNDG layer gets special handling (`SPLIT_MULTIPOINT=YES`, `ADD_SOUNDG_DEPTH=YES`) to produce individual depth points with a `DEPTH` property
4. **Consolidate** — merge per-chart GeoJSON into one file per layer in `data/merged/`
5. **tippecanoe** — builds one `.mbtiles` per zoom level in `data/tiles/` with `--no-tile-size-limit --no-feature-limit`
6. **tile-join** — merges all per-zoom tiles into one final `.mbtiles` (coarse bands first, detail wins on overlap)

All artifacts stored in `./data/` and preserved between runs for resume capability.

## Key Design Decisions

- **By-band mode** groups charts by NOAA usage band (1-6), not by state. Each band maps to a fixed zoom range (e.g., band 5 = harbour = z15-16). This is in `BAND_ZOOM` dict at the top of the file.
- **Resume-friendly**: tippecanoe skips zoom levels where a non-empty `.mbtiles` already exists; GDAL export skips if GeoJSON already exists in the band directory.
- **Skipped layers**: `DSID`, `C_AGGR`, `C_ASSO`, `Generic` are metadata-only and excluded from GDAL export.
- **Layer naming**: GeoJSON filenames like `DEPARE_US5MA1SK.geojson` — tippecanoe layer name comes from the part before the first underscore.

## CI / GitHub Actions

- `enc-sources.yaml` — defines all available builds (CG districts and individual states) with an `active` list controlling which run
- `.github/workflows/build-charts.yml` — downloads ENC ZIPs from NOAA, runs the pipeline, uploads `.mbtiles` as GitHub Release assets
- Manual trigger supports overriding the active build list and reusing cached GeoJSON

## Repo Structure

```
s57-to-mbtiles.py          # the tool
enc-sources.yaml            # CI build definitions
docs/
  INSTALL.md                # platform-specific setup (macOS, Raspberry Pi)
  USAGE.md                  # detailed usage guide, all CLI options
  SOUNDG-FIX.md             # depth sounding bug fix writeup
data/                       # gitignored working directory
  zips/  enc/  geojson/  merged/  tiles/
```

## Documentation

- `docs/INSTALL.md` — install guide for tippecanoe, GDAL, podman/docker (Raspberry Pi and macOS)
- `docs/USAGE.md` — detailed usage guide covering all five modes of operation and CLI options
- `docs/SOUNDG-FIX.md` — documents the SOUNDG depth sounding fix (both the tile generation bug and the Freeboard-SK rendering bug)
