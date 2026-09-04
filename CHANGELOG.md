# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- By-band mode shipped two charts in the same tiles at every zoom from 9
  to 16. The zoom extension and gap fills both relied on tile-join letting
  a later input win where tilesets overlap; tile-join instead merges
  same-layer features from all inputs into one layer, with a draw order
  that is not controllable and differs from tile to tile. Opaque depth
  areas from the coarser band therefore showed through in tile-shaped
  patches next to tiles showing the finer band (reported around Block
  Island, RI). The pipeline now enforces "finer chart wins" itself: before
  a band is tiled at zooms shared with a higher-priority source, the union
  of that source's chart footprints (`M_COVR`, `CATCOV=1`) is erased from
  its features with `ogr2ogr -clipsrc`, so a coarse chart is present only
  where no finer chart has coverage, as on an ECDIS. Each band now yields
  a native-zoom tileset and, where it overlaps a finer band, an erased
  extended-zoom tileset (`<band>_z<a>-<b>.minus-<finer>.mbtiles`).
- Same-band duplicates: NOAA's rescheming ships legacy and reschemed cells
  of one usage band that overlap with `M_COVR CATCOV=1` coverage in both and
  carry the same objects (an IHO S-57 App. B.1 §2.2 violation; verified in
  the raw `09CGD_ENCs.zip`). Consolidation concatenated both, so tiles held
  exact duplicate features (Chicago z10: 51; Block Island z10: 27; also
  Boston, Delaware Bay, Miami, San Diego, Honolulu). New Stage 2b resolves
  it before consolidation: within a band the reschemed cell wins (Design
  Handbook Annex A region codes, unambiguous for bands 1-2), and every layer
  of the legacy cell is clipped under the reschemed footprints into
  `data/geojson/<band>.resolved/`. Issue date and compilation scale were
  rejected as rules because both pick the legacy cell in measured cases.
  Bands 3-6 are detected and warned only. Each overlap is recorded in
  `data/merged/<band>/.same-band-overlaps.json` for reporting to NOAA.
  Research and evidence in `docs/SAME-BAND-OVERLAP.md`.
- Resume: a cell whose GeoJSON directory held an orphan layer file (an
  object class the cell no longer has after an ER update, or a leftover
  from an older export) never passed the freshness check, so it was
  re-exported on every run, which in turn re-consolidated and re-tiled
  its whole band. The export now clears all of a cell's outputs before
  rewriting it. In the 01CGD data 15 cells were in this state, forcing
  bands 3-5 to rebuild on every resume.
- Layer names were taken from the part of the filename before the first
  underscore, which folded every S-57 meta layer (`M_COVR`, `M_QUAL`,
  `M_NSYS`, `M_SDAT`, `M_VDAT`, `M_NPUB`) into one tile layer called `M`
  and `TS_FEB` into `TS`. Only a trailing NOAA cell name is stripped now,
  and stale merged layer files from the old naming are removed on the next
  consolidation.
- `check-tile-overlap.py`: diagnostic that reports tile addresses shared
  between tilesets per zoom and decodes a tile (or a lon/lat at several
  zooms) across all of them, showing which layers each contributes.

### Changed
- Gap fills are render sources with priority `cellband - 0.1` and go
  through the same erase rule. At each zoom the band NOAA compiled for
  that zoom outranks everything, then finer data wins, so a fill appears
  only where the natural pipeline has no chart and is fully erased
  wherever its own band renders. Fills of the same cell band do not erase
  each other.
- Runs whose every layer was erased are skipped instead of being handed
  to tippecanoe, which exits non-zero on empty input.
- GDAL must provide the SQLite dialect's spatial functions (`ST_Union`,
  `ST_Difference`) for by-band mode: a build with SpatiaLite or GEOS.
  Ubuntu `gdal-bin` (CI) and Homebrew `gdal` qualify. The alpine-small
  container image has not been verified.
- `trim_low_bands_to_region` operates on a (priority, path) list so a
  band can contribute more than one tileset.
- `build_clip_complement` now takes footprint files, a label and a mask
  directory (shared by the band-level erase and Stage 2b);
  `consolidate_geojson` accepts per-cell file overrides; gap-fill cell lookup
  only scans `bandN` directories, not `bandN.resolved`.
- `verify-r2-charts.py`: downloads each published district file from R2 one
  at a time, measures coverage gaps and stacking at probe points, and writes
  a Markdown report. Alaska is excluded unless named.

## [0.6.0] - 2026-09-02

### Added
- `BAND_ZOOM_EXTENSION`: in by-band mode each band renders two zoom levels
  past its native ceiling (capped at `--maxzoom`), so band 4 reaches z16 and
  fills deep zooms wherever no harbour chart exists. Overview bands stop
  early; rendering band 1 to z16 blew CI time and disk on Pacific districts.
- Gap fills: named groups of higher-band cells in `enc-sources.yaml`
  (`gap_fills:`) rendered at a lower zoom range to cover NOAA legacy-ENC
  coverage holes. Output slots between band 2 and band 3 in tile-join.
  Groups added for Gulf of Maine, Downeast Maine, and other documented gaps.
- By-band mode clips band 1/2 (overview/general) output to the district's
  regional extent. The region is the union of the district's own band 3+
  chart footprints, rasterized as a z11 tile mask and dilated by one tile,
  so multi-part districts (e.g. Hawaii + Guam + Samoa) are handled without
  a single bounding box. Ocean-basin overview cells such as US1PO02M no
  longer plant planet-wide tiles and -180..180 bounds in district files.
  The clip is written to a derived `<band>.region.mbtiles` and never
  modifies the cached tippecanoe output, so resuming after adding inputs
  that widen the region still yields full overview coverage.
- Heavy layers (`SOUNDG`, lights, buoys, beacons, obstructions, wrecks,
  rocks) start one zoom level above their band's bottom zoom, via a
  per-feature `tippecanoe.minzoom` stamp applied during consolidation
  (`LAYER_MIN_ZOOM_OFFSET`). tippecanoe silently ignores per-layer minzoom
  in the `-L` spec, so the feature-level stamp is the only form it honors.
- `find-band-holes.py`: stdlib tool that reads the NOAA ENC product
  catalog and reports, per district, where lower-band cells are not
  covered by higher-band cells (i.e. where the tile pyramid goes blank).
- CI: Cloudflare R2 mirror for every chart file, with release notes linking
  to the R2 copies; handles an in-bucket folder path and a trailing slash
  in the bucket secret.
- CI: `restore-release.yml` one-shot workflow to re-attach artifacts from a
  prior run to a release without downloading them locally.
- CI: active builds expanded to Coast Guard districts 05, 07, 08, 09, 11,
  13, 14. District 17 (Alaska) is listed but disabled: its 2.3 GB output
  exceeds GitHub's 2 GiB release-asset limit.

### Changed
- tippecanoe runs once per band/source over its full zoom range instead of
  once per zoom level. Merged layers are passed as separate `-L` layers.
- Every stage is incremental by modification time: GDAL re-exports only
  cells whose `.000` or update files are newer than their GeoJSON,
  consolidation rebuilds only layers with newer inputs (or when the heavy-
  layer stamp config changed), and a band's `.mbtiles` is reused only if
  it is newer than every merged layer and its stored zoom range matches.
- The region mask reads only each band's bottom-zoom tile rows instead of
  every row in the file, avoiding multi-million-row reads from band 4/5
  files rendered to z16.
- Version is now tracked only in `pyproject.toml`. The hardcoded
  `__version__` string and the `--version` flag are removed from the script.
- CI: release job runs even when some matrix builds fail and uploads
  assets one at a time, so one oversized or failed asset cannot abort the
  rest. Assets over 2 GiB are skipped with a warning; assets from districts
  not rebuilt this run are preserved.
- CI: workflow pins `actions/checkout@v7`, `actions/upload-artifact@v7`,
  and `actions/download-artifact@v8` (Node 24 runtime), clearing the
  Node 20 deprecation warning.
- `docs/USAGE.md` rewritten to match the current pipeline (data directory
  layout, single tippecanoe run per band, freshness rules, options).

## [0.5.0] - 2026-03-28

### Added
- Five-stage conversion pipeline: extract, GDAL export, consolidate, tippecanoe, tile-join
- By-band mode (`--by-band`) for multi-state builds grouped by NOAA usage band
- Two-source split mode (`--split`) for coarse + detail merging
- Multi-source mode (`--sources`) with explicit zoom ranges per input
- GeoJSON reuse mode (`--geojson-dir`) to skip the slow GDAL step on re-runs
- Resume support: skips zoom levels where tiles already exist
- Parallel processing (`-j` flag) for GDAL export, GeoJSON consolidation, and tippecanoe
- Native GDAL support alongside containerized GDAL (Docker/Podman)
- SOUNDG depth sounding fix: `SPLIT_MULTIPOINT=YES` + `ADD_SOUNDG_DEPTH=YES` for individual depth points
- GeoJSON consolidation stage to merge per-chart layers before tiling
- `enc-sources.yaml` config for CI builds with active list
- GitHub Actions workflow for automated NOAA chart builds
- `--version` flag
- Installation guide for macOS and Raspberry Pi
- Detailed usage guide covering all five modes
- SOUNDG fix writeup documenting the depth sounding bug and solution
- CI: per-build metadata JSON sidecar generated alongside each `.mbtiles`
  and uploaded with it
- MIT license
