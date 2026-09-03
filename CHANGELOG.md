# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
