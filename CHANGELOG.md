# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
