# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This workspace covers two related projects:

1. **`s57-to-mbtiles.py`** — Single-script Python tool that converts NOAA S-57 ENC nautical charts (`.000` files) into vector MBTiles for use with SignalK / Freeboard-SK.
2. **Freeboard-SK** (`../freeboard-sk/`) — Angular-based chart plotter webapp for Signal K marine servers that consumes these tiles.

---

## s57-to-mbtiles.py

### External Dependencies

- **podman or docker** — runs the GDAL container (`ghcr.io/osgeo/gdal:alpine-small-latest`) for `ogr2ogr` conversions
- **tippecanoe + tile-join** — converts GeoJSON to vector `.mbtiles` tiles (native install)
- **Python 3** — standard library only

### Running

```bash
# Single source
./s57-to-mbtiles.py NY_ENCs.zip

# By-band mode (recommended for multi-state)
./s57-to-mbtiles.py CT.zip RI.zip MA.zip NY.zip --by-band -o merged.mbtiles

# Reuse existing GeoJSON (skip slow GDAL step)
./s57-to-mbtiles.py --geojson-dir /tmp/s57_xxxx/geojson/ --minzoom 9 --maxzoom 16
```

There are no tests or linting configured for this project.

### Pipeline Architecture

The script is a single-file pipeline with five stages per band/source:

1. **Extract** — unzip inputs into temp staging area
2. **Find** — discover `.000` ENC files (group by NOAA band in `--by-band` mode)
3. **GDAL export** — `ogr2ogr` via container converts each ENC layer to GeoJSON. SOUNDG layer gets special handling (`SPLIT_MULTIPOINT=YES`, `ADD_SOUNDG_DEPTH=YES`) to produce individual depth points with a `DEPTH` property
4. **tippecanoe** — builds one `.mbtiles` per zoom level with `--no-tile-size-limit --no-feature-limit`
5. **tile-join** — merges all per-zoom tiles into one final `.mbtiles` (coarse bands first, detail wins on overlap)

Temp files go to `/tmp/s57_XXXXXXXX/` and are kept by default (the GDAL step is the slowest; keeping GeoJSON enables re-runs via `--geojson-dir`).

### Key Design Decisions

- **By-band mode** groups charts by NOAA usage band (1-6), not by state. Each band maps to a fixed zoom range (e.g., band 5 = harbour = z15-16). This is in `BAND_ZOOM` dict at the top of the file.
- **Resume-friendly**: tippecanoe skips zoom levels where a non-empty `.mbtiles` already exists in the temp tiles directory.
- **Skipped layers**: `DSID`, `C_AGGR`, `C_ASSO`, `Generic` are metadata-only and excluded from GDAL export.
- **Layer naming**: GeoJSON filenames like `DEPARE_US5MA1SK.geojson` — tippecanoe layer name comes from the part before the first underscore.

### Companion Documentation

- `S57-CONVERT-INSTALL.md` — install guide for tippecanoe, podman/docker, and GDAL image (Raspberry Pi and macOS)
- `S57-TO-MBTILES-GUIDE.md` — detailed usage guide covering all five modes of operation and CLI options
- `SOUNDG-FIX-WRITEUP.md` — documents the SOUNDG depth sounding fix (both the tile generation bug and the Freeboard-SK rendering bug in `s57Style.ts`)

---

## Freeboard-SK (`../freeboard-sk/`)

### Commands

```bash
# Install dependencies
npm i

# Development server (http://localhost:4200)
npm start          # or: ng serve

# Run tests (vitest via Angular CLI)
npm test           # or: ng test

# Run a single test file
npx vitest run src/app/modules/map/ol/lib/charts/zoom-utils.spec.ts

# Format code
npm run format         # src/**/*.{ts,html}
npm run format:helper  # helper/**/*.{ts,html}
npm run format:all     # both

# Build for production (webapp + helper plugin)
npm run build:prod

# Build individually
npm run build:web      # webapp only → /public
npm run build:helper   # helper plugin only → /plugin
```

### Architecture

**Two deliverables in one repo:**

1. **Webapp** (`src/`) — Angular SPA, the chart plotter UI. Built with `ng build`, output goes to `public/`.
2. **Helper plugin** (`helper/`) — Node/Express Signal K server plugin (alarm endpoints). Built with `tsc -p tsconfig-helper.json`, output goes to `plugin/`.

**Webapp structure (`src/app/`):**

- **`app.facade.ts`** — central application service. Manages app state, Signal K connection, configuration. In dev mode, uses the `DEV_SERVER` object to connect to a configurable Signal K host instead of the browser's host.
- **`app.config.ts`** — config shape (`IAppConfig`), defaults, validation, and migration logic.
- **`types/`** — shared TypeScript interfaces.
- **`modules/`** — feature modules:
  - `map/` — main map display using OpenLayers. `ol/lib/` contains OL layer components for each chart type (S57, raster, vector, WMS, WMTS, PMTiles) and the S57 styling engine (`s57Style.ts`, `s57.service.ts`).
  - `skresources/` — routes, waypoints, notes, regions, tracks, charts.
  - `skstream/` — Signal K WebSocket stream handling.
  - `course/` — active route / destination management.
  - `alarms/`, `autopilot/`, `settings/`, `weather/`, `radar/`, `buddies/`, `experiments/`, `gpx/` — additional feature areas.
- **`lib/`** — shared utilities: `services/` (IndexedDB, state, wakelock), dialog components, unit conversions (`convert.ts`), geographic calculations (`geoutils.ts`), XML web worker.

**S57 chart rendering** (`src/app/modules/map/ol/lib/charts/`):
- `s57Style.ts` — style functions mapping S-57 feature attributes to OpenLayers styles (depth areas, soundings, buoys, lights, etc.)
- `s57.service.ts` — S57 state (shallow/safety/deep depth thresholds)
- `layer-s57-chart.component.ts` — Angular/OL component for S57 vector tile layers

**Key technologies:** Angular 21, OpenLayers, Angular Material, signalk-client-angular, Vitest, Prettier, SCSS.
