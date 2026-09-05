# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Single-script Python tool (`s57-to-mbtiles.py`) that converts NOAA S-57 ENC nautical charts (`.000` files) into vector MBTiles for use with SignalK / Freeboard-SK.

There are no tests or linting configured for this project.

## External Dependencies

- **GDAL** (`ogr2ogr`, `ogrinfo`) — native install or via container (`ghcr.io/osgeo/gdal:alpine-small-latest`) using podman/docker. By-band mode also needs the SQLite dialect's spatial functions (`ST_Union`, `ST_Difference`), i.e. a GDAL built with SpatiaLite or GEOS; Ubuntu's `gdal-bin` and Homebrew's `gdal` both qualify (verified Sept 2026)
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
2. **Find** — discover `.000` ENC files (group by NOAA band in `--by-band` mode), then **drop cancelled cells**: a cell whose DSID edition number is 0 after updates has been withdrawn by NOAA (S-57 App. B.1 §5.7 cancellation update) but still ships in the district zips; 190 such cells across the eight active districts on 2026-09-05. Excluded cells are listed in `data/cancelled-cells.json`; stale exports of them are removed
3. **GDAL export** — `ogr2ogr` (native or container) converts each ENC layer to GeoJSON in `data/geojson/`. SOUNDG layer gets special handling (`SPLIT_MULTIPOINT=YES`, `ADD_SOUNDG_DEPTH=YES`) to produce individual depth points with a `DEPTH` property
3b. **Same-band overlap resolution** (by-band only) — detects live cells of one band whose `M_COVR` coverage overlaps and records every pair in `data/merged/<band>/.same-band-overlaps.json`. After cancelled cells are dropped the only known case is legacy `US2EC03M` under reschemed `US2ATL*` cells; a pair is resolved (reschemed wins, legacy clipped into `data/geojson/<band>.resolved/`) only when exactly one side carries an Annex A band 1-2 region code, everything else is a warning. See `docs/SAME-BAND-OVERLAP.md`
4. **Consolidate** — merge per-chart GeoJSON into one file per layer in `data/merged/`
5. **Erase** (by-band only) — for zooms where a band overlaps a finer band, `ogr2ogr -clipsrc` removes the finer band's chart footprints (`M_COVR`, `CATCOV=1`) from its features, into `data/merged/<band>.minus-<finer>/`
6. **tippecanoe** — builds one `.mbtiles` per band and zoom group in `data/tiles/` with `--no-tile-size-limit --no-feature-limit`
7. **tile-join** — merges all tilesets into one final `.mbtiles`. tile-join does **not** let a later input win: overlapping layers are merged feature-by-feature, which is why the erase stage exists

All artifacts stored in `./data/` and preserved between runs for resume capability.

## Key Design Decisions

- **By-band mode** groups charts by NOAA usage band (1-6), not by state. Each band starts at its native minzoom and renders `BAND_ZOOM_EXTENSION` (=2) zoom levels past its native ceiling, capped at the global maxzoom — so band 4 (native z13-14) reaches z16, filling deep zooms along the whole charted coast, while ocean-basin overview bands stop early (rendering band 1 to z16 blew CI time and disk on Pacific districts). **Finer chart wins on overlap, and the pipeline has to enforce that itself**: tile-join merges same-layer features from overlapping inputs rather than replacing them, so at every zoom where a band shares zooms with a higher-priority source (the next band, or a gap fill of finer cells), the union of those sources' `M_COVR` footprints is erased from its features before tippecanoe (`plan_render_runs`, `erase_for_run`). The erased copy is tiled separately from the band's native-zoom run, so each band yields up to two tilesets. Gap fills carry priority cellband − 0.1 and go through the same rule; at each zoom the band NOAA compiled for that zoom outranks everything (`priority_at`), so fills and extensions show only where the native chart has no coverage. Native ranges are in the `BAND_ZOOM` dict at the top of the file. Band 1/2 output is then clipped to the district's regional extent — the union of the district's own band ≥3 footprints as a z11 tile mask (`trim_low_bands_to_region`) — because ocean-basin overview cells (e.g. US1PO02M, the whole North Pacific) otherwise put planet-wide tiles and −180..180 bounds into every district file. The clip never touches the cached tippecanoe output: it writes a derived `*.region.mbtiles` copy that is regenerated every run and is what tile-join consumes, so resume stays correct when a later run's band ≥3 inputs widen the region.
- **Resume-friendly**: tippecanoe skips zoom levels where a non-empty `.mbtiles` already exists; GDAL export skips if GeoJSON already exists in the band directory.
- **Skipped layers**: `DSID`, `C_AGGR`, `C_ASSO`, `Generic` are metadata-only and excluded from GDAL export. `DSID` is still read per cell (`read_cell_dsid`) for the cancellation check; it is the only record that says whether a cell is alive.
- **Layer naming**: GeoJSON filenames like `DEPARE_US5MA1SK.geojson` — the tippecanoe layer name is the stem with a trailing NOAA cell name removed (`layer_name_from_stem`). Layer names can contain underscores (`M_COVR`, `M_QUAL`, `TS_FEB`), so never split on the first one.

## CI / GitHub Actions

- `enc-sources.yaml` — defines all available builds (CG districts and individual states) with an `active` list controlling which run
- `.github/workflows/build-charts.yml` — downloads ENC ZIPs from NOAA, runs the pipeline, uploads `.mbtiles` as GitHub Release assets
- Manual trigger supports overriding the active build list and reusing cached GeoJSON

## Repo Structure

```
s57-to-mbtiles.py          # the tool
check-tile-overlap.py       # diagnostic: shared tile addresses between tilesets, per-tile layer counts
check-tile-duplicates.py    # diagnostic: chart cells stacked and exact duplicate features per tile in a merged file
verify-r2-charts.py         # downloads each published district file, measures coverage gaps and stacking, writes a report
pyproject.toml              # project metadata; the version lives here and nowhere else
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
- `docs/SAME-BAND-OVERLAP.md` — NOAA ships legacy and reschemed cells of the same band with overlapping data (an S-57 App. B.1 §2.2 violation); evidence, standards, NOAA's stated intent, how OpenCPN and others cope, and the Stage 2b mechanism (reschemed wins, bands 1-2)
